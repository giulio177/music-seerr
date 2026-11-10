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
from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, ID3NoHeaderError
from mutagen.mp3 import MP3

def sanitize_filename(name: str) -> str:
    """Convert user-provided metadata into one safe filesystem component."""
    safe = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "", str(name)).strip().strip(".")
    safe = re.sub(r"\.{2,}", ".", safe)
    return (safe[:150] or "Unknown")

class DownloadManager:
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
                    message="Download interrotto dal riavvio. Puoi riprovare.",
                    created_at=row.get("created_at"),
                )
                self.jobs[job.job_id] = job
            except Exception as exc:
                print(f"[DownloadManager] Could not restore interrupted job: {exc}")

    def get_job(self, job_id: str) -> Optional[DownloadJob]:
        return self.jobs.get(job_id)

    def list_jobs(self, owner_id: Optional[str] = None, include_all: bool = False, user_id: Optional[str] = None):
        jobs = list(self.jobs.values())
        if include_all:
            if user_id:
                return [job for job in jobs if job.owner_user_id == user_id]
            return jobs
        return [job for job in jobs if job.owner_user_id == owner_id]

    async def create_job(self, track: TrackItem, playlist_id: Optional[str] = None, playlist_name: Optional[str] = None, owner: Optional[dict] = None, playlist_user_id: Optional[str] = None) -> DownloadJob:
        active = sum(job.status in {"pending", "downloading", "tagging", "jellyfin_sync"} for job in self.jobs.values())
        if active >= 10:
            raise HTTPException(status_code=429, detail="Coda download piena: attendi il completamento di un brano.")
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
            message="Richiesta in coda"
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
            job.message = f"Errore durante il download: {exc}"
            UserDataService.update_download_status(job.job_id, job.status)

    async def _run_download(self, job: DownloadJob):
        track = job.track
        job.status = "downloading"
        UserDataService.update_download_status(job.job_id, job.status)
        job.progress = 10
        job.message = f"Ricerca audio per {track.artist} - {track.title}"

        safe_artist = sanitize_filename(track.artist)
        safe_album = sanitize_filename(track.album)
        safe_title = sanitize_filename(track.title)

        music_root = os.path.realpath(settings.MUSIC_OUTPUT_DIR)
        dest_dir = os.path.realpath(os.path.join(music_root, safe_artist, safe_album))
        if os.path.commonpath([music_root, dest_dir]) != music_root:
            raise ValueError("Percorso di destinazione non valido")
        os.makedirs(dest_dir, exist_ok=True)
        final_mp3 = os.path.join(dest_dir, f"{safe_title}.mp3")

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
            "--output", os.path.join(dest_dir, f"{safe_title}.%(ext)s"),
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
            self._tag_audio(final_mp3, track)

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
                job.message = "Aggiornamento libreria Jellyfin in corso..."
                await JellyfinService.refresh_library()

                # Se l'utente ha chiesto di aggiungere a una playlist
                if job.playlist_id:
                    job.message = f"Attesa scansione per inserire in playlist '{job.playlist_name or job.playlist_id}'..."
                    # Polling fino a 15 secondi per attendere che Jellyfin veda il brano
                    jellyfin_item_id = None
                    for _ in range(8):
                        await asyncio.sleep(2)
                        jellyfin_item_id = await JellyfinService.find_track_id(track.title, track.artist)
                        if jellyfin_item_id:
                            break
                    
                    if jellyfin_item_id:
                        await JellyfinService.add_to_playlist(job.playlist_id, jellyfin_item_id, user_id=job.playlist_user_id)
                        job.message = f"Aggiunto con successo alla playlist!"
                    else:
                        job.message = "File scaricato, ma non ancora indicizzato nella playlist da Jellyfin."

            job.status = "completed"
            job.progress = 100
            UserDataService.update_download_status(job.job_id, job.status)
            if not job.message or "Attesa" in job.message:
                job.message = f"Completato! Salvato in {safe_artist}/{safe_album}/{safe_title}.mp3"

        except Exception as e:
            print(f"[DownloadManager] Errore: {e}")
            job.status = "failed"
            UserDataService.update_download_status(job.job_id, job.status)
            job.message = f"Errore durante il download: {str(e)}"

    def _tag_audio(self, mp3_path: str, track: TrackItem):
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

            # Se c'è una copertina, scaricala e incorporala nell'MP3
            if track.cover_url:
                try:
                    parsed = urlsplit(track.cover_url)
                    host = (parsed.hostname or "").lower()
                    allowed_host = host == "e-cdns-images.dzcdn.net" or host.endswith(".mzstatic.com")
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
