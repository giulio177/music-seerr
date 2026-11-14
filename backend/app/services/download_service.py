import os
import sys
import re
import uuid
import asyncio
import urllib.request
from urllib.parse import urlsplit
from typing import Dict, Optional
from fastapi import HTTPException
from app.config import settings
from app.models.schemas import TrackItem, DownloadJob
from app.services.jellyfin_service import JellyfinService
from app.services.lyrics_service import LyricsService
from app.services.user_data_service import UserDataService
import mutagen
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TRCK, TPOS, TDRC, TSRC, APIC, ID3NoHeaderError
from mutagen.mp3 import MP3
from app.services.library_layout import ReleaseInfo, resolve_release, album_directory, track_stem, find_existing_track

class DownloadManager:
    MAX_ACTIVE_JOBS = 500
    ACTIVE_STATUSES = {"pending", "downloading", "tagging", "jellyfin_sync"}

    def __init__(self):
        self.jobs: Dict[str, DownloadJob] = {}
        self._download_slots = asyncio.Semaphore(2)
        self._restore_interrupted_jobs()

    def _restore_interrupted_jobs(self):
        """Restore interrupted records to the visible queue after a process restart."""
        for row in UserDataService.list_downloads(limit=500):
            if row.get("status") != "interrupted":
                continue
            try:
                job = DownloadJob(
                    job_id=row["job_id"],
                    track=TrackItem(**row["track"]),
                    playlist_id=row.get("playlist_id"),
                    playlist_name=row.get("playlist_name"),
                    owner_user_id=row.get("user_id"),
                    owner_name=row.get("user_name"),
                    status="interrupted",
                    progress=0,
                    message="Download interrupted by a server restart. You can retry it.",
                    created_at=row.get("created_at"),
                )
                self.jobs[job.job_id] = job
            except Exception as exc:
                print(f"[DownloadManager] Could not restore interrupted job: {exc}")

    def get_job(self, job_id: str) -> Optional[DownloadJob]:
        return self.jobs.get(job_id)

    def list_jobs(self, owner_id: Optional[str] = None, include_all: bool = False, user_id: Optional[str] = None):
        # Completed work belongs in persistent download history, not the live queue.
        jobs = [job for job in self.jobs.values() if job.status in self.ACTIVE_STATUSES | {"failed", "interrupted"}]
        if include_all:
            if user_id:
                return [job for job in jobs if job.owner_user_id == user_id]
            return jobs
        return [job for job in jobs if job.owner_user_id == owner_id]

    async def create_job(self, track: TrackItem, playlist_id: Optional[str] = None, playlist_name: Optional[str] = None, owner: Optional[dict] = None, playlist_user_id: Optional[str] = None) -> DownloadJob:
        active = sum(job.status in self.ACTIVE_STATUSES for job in self.jobs.values())
        if active >= self.MAX_ACTIVE_JOBS:
            raise HTTPException(status_code=429, detail="The queue has reached its 500 download limit. Wait for some tracks to finish.")
        owner = owner or {"id": "guest:unknown", "name": "Guest", "is_admin": False}
        job_id = str(uuid.uuid4())
        job = DownloadJob(
            job_id=job_id,
            track=track,
            playlist_id=playlist_id,
            playlist_name=playlist_name,
            playlist_user_id=playlist_user_id,
            owner_user_id=owner.get("id"),
            owner_name=owner.get("name", "Guest"),
            status="pending",
            progress=0,
            message="Request queued"
        )
        self.jobs[job_id] = job
        track_data = track.model_dump() if hasattr(track, "model_dump") else track.dict()
        UserDataService.record_download(job_id, owner, track_data, playlist_id, playlist_name)
        
        # Avvia il download in background asincrono
        asyncio.create_task(self._process_download(job))
        return job

    async def _process_download(self, job: DownloadJob):
        try:
            async with self._download_slots:
                await self._run_download(job)
        except Exception as exc:
            print(f"[DownloadManager] Job {job.job_id} failed before execution: {exc}")
            job.status = "failed"
            job.message = f"Download failed: {exc}"
            UserDataService.update_download_status(job.job_id, job.status)

    async def _run_download(self, job: DownloadJob):
        track = job.track
        job.status = "downloading"
        UserDataService.update_download_status(job.job_id, job.status)
        job.progress = 10
        job.message = f"Finding audio for {track.artist} - {track.title}"

        # Lidarr-style layout: Album Artist/Album (Year)/Album Artist - Album - NN - Title.mp3
        release = await resolve_release(track)
        music_root = os.path.realpath(settings.MUSIC_OUTPUT_DIR)
        dest_dir = album_directory(music_root, release)
        stem = track_stem(release)
        final_mp3 = os.path.join(dest_dir, f"{stem}.mp3")
        relative_path = os.path.relpath(final_mp3, music_root)

        existing = find_existing_track(dest_dir, release)
        if existing:
            job.file_path = existing
            job.status = "completed"
            job.progress = 100
            job.message = f"Already in the library: {os.path.relpath(existing, music_root)}"
            UserDataService.update_download_status(job.job_id, job.status)
            return
        os.makedirs(dest_dir, exist_ok=True)

        # Cerca la traccia audio su YouTube Music / YouTube
        search_query = f"ytsearch1:{track.artist} - {track.title} audio"
        cmd = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "0",
            "--no-cache-dir",
            "--output", os.path.join(dest_dir, stem.replace("%", "%%") + ".%(ext)s"),
            "--no-playlist",
            "--default-search", "ytsearch",
            search_query
        ]

        try:
            job.progress = 30
            job.message = "Download stream audio in corso..."
            
            # Esegui il processo yt-dlp
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=3600)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise TimeoutError("Download interrotto: superato il limite di un'ora")

            if proc.returncode != 0:
                raise Exception(f"yt-dlp error: {stderr.decode()[:300]}")

            job.status = "tagging"
            UserDataService.update_download_status(job.job_id, job.status)
            job.progress = 70
            job.message = "Applicazione metadati e copertina HD..."

            # Applica ID3 Tags con Mutagen per renderlo perfetto per Jellyfin
            self._tag_audio(final_mp3, release)

            # Ricerca automatica Lyrics sincronizzate (LRC) e plain
            try:
                job.message = "Ricerca testi sincronizzati (LRC)..."
                lyrics_data = await LyricsService.fetch_lyrics(
                    title=track.title,
                    artist=track.artist,
                    album=track.album,
                    duration=track.duration
                )
                if lyrics_data:
                    if lyrics_data.get("synced"):
                        LyricsService.save_lrc_file(final_mp3, lyrics_data["synced"])
                    if lyrics_data.get("plain"):
                        LyricsService.embed_lyrics_id3(final_mp3, lyrics_data["plain"])
            except Exception as lyr_err:
                print(f"[DownloadService] Errore salvataggio lyrics: {lyr_err}")

            job.file_path = final_mp3
            job.progress = 85

            # Notifica e sincronizzazione con Jellyfin
            if settings.JELLYFIN_API_KEY:
                job.status = "jellyfin_sync"
                UserDataService.update_download_status(job.job_id, job.status)
                job.message = "Refreshing the Jellyfin library..."
                await JellyfinService.refresh_library()

                # Se l'utente ha chiesto di aggiungere a una playlist
                if job.playlist_id:
                    job.message = f"Waiting for Jellyfin to scan the track before adding it to '{job.playlist_name or job.playlist_id}'..."
                    # Polling fino a 15 secondi per attendere che Jellyfin veda il brano
                    jellyfin_item_id = None
                    for _ in range(8):
                        await asyncio.sleep(2)
                        jellyfin_item_id = await JellyfinService.find_track_id(track.title, track.artist)
                        if jellyfin_item_id:
                            break
                    
                    if jellyfin_item_id:
                        await JellyfinService.add_to_playlist(job.playlist_id, jellyfin_item_id, user_id=job.playlist_user_id)
                        job.message = "Added to the playlist."
                    else:
                        job.message = "The file was downloaded, but Jellyfin has not added it to the playlist yet."

            job.status = "completed"
            job.progress = 100
            UserDataService.update_download_status(job.job_id, job.status)
            if not (job.playlist_id and settings.JELLYFIN_API_KEY) or "Waiting for Jellyfin" in job.message:
                job.message = f"Completed. Saved to {relative_path}"

        except Exception as e:
            print(f"[DownloadManager] Errore: {e}")
            job.status = "failed"
            UserDataService.update_download_status(job.job_id, job.status)
            job.message = f"Download failed: {str(e)}"

    def _tag_audio(self, mp3_path: str, track: ReleaseInfo):
        """Inietta metadati ID3 e copertina ad altissima risoluzione nell'MP3"""
        if not os.path.exists(mp3_path):
            return

        try:
            try:
                audio = MP3(mp3_path, ID3=ID3)
            except ID3NoHeaderError:
                audio = MP3(mp3_path)
                audio.add_tags()

            audio.tags.add(TIT2(encoding=3, text=track.title))
            audio.tags.add(TPE1(encoding=3, text=track.artist))
            audio.tags.add(TALB(encoding=3, text=track.album))
            audio.tags.add(TPE2(encoding=3, text=track.album_artist))
            if track.track_number:
                audio.tags.add(TRCK(encoding=3, text=str(track.track_number)))
            if track.disc_number:
                audio.tags.add(TPOS(encoding=3, text=str(track.disc_number)))
            if track.year:
                audio.tags.add(TDRC(encoding=3, text=str(track.year)))
            if track.isrc:
                audio.tags.add(TSRC(encoding=3, text=track.isrc))

            # Se c'è una copertina, scaricala e incorporala nell'MP3
            if track.cover_url:
                try:
                    parsed = urlsplit(track.cover_url)
                    host = (parsed.hostname or "").lower()
                    allowed_host = host.endswith(".dzcdn.net") or host.endswith(".mzstatic.com")
                    if parsed.scheme != "https" or not allowed_host or parsed.username or parsed.password:
                        raise ValueError("Host copertina non consentito")
                    req = urllib.request.Request(
                        track.cover_url,
                        headers={'User-Agent': 'Mozilla/5.0'}
                    )
                    class NoRedirect(urllib.request.HTTPRedirectHandler):
                        def redirect_request(self, req, fp, code, msg, headers, newurl):
                            return None

                    opener = urllib.request.build_opener(NoRedirect)
                    with opener.open(req, timeout=5) as response:
                        image_data = response.read(8 * 1024 * 1024 + 1)
                        if len(image_data) > 8 * 1024 * 1024:
                            raise ValueError("Copertina troppo grande")
                        audio.tags.add(
                            APIC(
                                encoding=3,
                                mime='image/jpeg',
                                type=3,  # 3 = front cover
                                desc='Cover',
                                data=image_data
                            )
                        )
                except Exception as img_err:
                    print(f"[Tagging] Errore copertina: {img_err}")

            audio.save()
        except Exception as e:
            print(f"[Tagging] Errore salvataggio ID3: {e}")

download_manager = DownloadManager()
