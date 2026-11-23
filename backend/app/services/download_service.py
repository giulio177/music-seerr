import os
import sys
import re
import uuid
import asyncio
import shutil
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
from app.services import file_replace_service
from app.services.library_layout import ReleaseInfo, resolve_release, find_cover_url, album_directory, track_stem, find_existing_track

class DownloadManager:
    MAX_ACTIVE_JOBS = 500
    ACTIVE_STATUSES = {"pending", "downloading", "tagging", "jellyfin_sync"}

    def __init__(self):
        self.jobs: Dict[str, DownloadJob] = {}
        self._download_slots = asyncio.Semaphore(2)
        self._procs: Dict[str, asyncio.subprocess.Process] = {}
        self._cancelled: set = set()
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

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a queued or downloading job; later stages finish normally."""
        job = self.jobs.get(job_id)
        if not job or job.status not in {"pending", "downloading"}:
            return False
        self._cancelled.add(job_id)
        proc = self._procs.get(job_id)
        if proc and proc.returncode is None:
            proc.kill()
        if job.status == "pending":
            self._mark_cancelled(job)
        return True

    def _mark_cancelled(self, job: DownloadJob):
        job.status = "cancelled"
        job.message = "Download cancelled."
        UserDataService.update_download_status(job.job_id, job.status)

    def list_jobs(self, owner_id: Optional[str] = None, include_all: bool = False, user_id: Optional[str] = None):
        # Completed work belongs in persistent download history, not the live queue.
        jobs = [job for job in self.jobs.values() if job.status in self.ACTIVE_STATUSES | {"failed", "interrupted"}]
        if include_all:
            if user_id:
                return [job for job in jobs if job.owner_user_id == user_id]
            return jobs
        return [job for job in jobs if job.owner_user_id == owner_id]

    async def create_job(self, track: TrackItem, playlist_id: Optional[str] = None, playlist_name: Optional[str] = None, owner: Optional[dict] = None, playlist_user_id: Optional[str] = None, force: bool = False, replace: Optional[dict] = None) -> DownloadJob:
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
            message="Request queued",
            force=force,
            replace_path=(replace or {}).get("path"),
            replace_item_id=(replace or {}).get("item_id"),
            replace_candidate=(replace or {}).get("candidate"),
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
                if job.job_id in self._cancelled:
                    return
                await self._run_download(job)
        except Exception as exc:
            print(f"[DownloadManager] Job {job.job_id} failed before execution: {exc}")
            job.status = "failed"
            job.message = f"Download failed: {exc}"
            UserDataService.update_download_status(job.job_id, job.status)

    async def _run_download(self, job: DownloadJob):
        if job.replace_path:
            await self._run_replace(job)
            return
        track = job.track
        job.status = "downloading"
        UserDataService.update_download_status(job.job_id, job.status)
        job.progress = 10
        job.message = f"Finding audio for {track.artist} - {track.title}"

        # Lidarr-style layout: Album Artist/Album (Year)/Album Artist - Album - NN - Title.mp3
        release = await resolve_release(track)
        if not release.cover_url:
            release.cover_url = await find_cover_url(track)
        if release.cover_url and not track.cover_url:
            track.cover_url = release.cover_url
            UserDataService.update_download_cover(job.job_id, release.cover_url)
        music_root = os.path.realpath(settings.MUSIC_OUTPUT_DIR)
        dest_dir = album_directory(music_root, release)
        stem = track_stem(release)
        existing = find_existing_track(dest_dir, release)
        if existing and job.force:
            # "Not this song": keep the existing file and save the download under a free name next to it.
            stem = _free_stem(dest_dir, stem)
            existing = None
        final_mp3 = os.path.join(dest_dir, f"{stem}.mp3")
        relative_path = os.path.relpath(final_mp3, music_root)

        if existing:
            job.file_path = existing
            job.existing_path = os.path.relpath(existing, music_root)
            job.status = "completed"
            job.progress = 100
            job.message = f"Already in the library: {os.path.relpath(existing, music_root)}"
            UserDataService.update_download_status(job.job_id, job.status)
            # The song is already there: still put it in the chosen playlist.
            if job.playlist_id and settings.JELLYFIN_API_KEY:
                asyncio.create_task(self._add_to_playlist_when_ready(job, existing))
            return
        os.makedirs(dest_dir, exist_ok=True)

        # Cerca la traccia audio su YouTube Music / YouTube
        # Prefer the upload whose length matches the catalog (no video intros/outros); fall back to the first hit.
        job.message = "Choosing the version with the right length..."
        search_query = await file_replace_service.pick_source(track.artist, track.title, int(track.duration or 0)) \
            or f"ytsearch1:{track.artist} - {track.title} audio"
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
            # No catalog artwork: embed the video thumbnail, cropped square, so the file never lacks a cover.
            *([] if release.cover_url else [
                "--embed-thumbnail", "--convert-thumbnails", "jpg",
                "--ppa", "ThumbnailsConvertor+ffmpeg_o:-c:v mjpeg -vf crop=\"'if(gt(ih,iw),iw,ih)':'if(gt(iw,ih),ih,iw)'\"",
            ]),
            "--default-search", "ytsearch",
            search_query
        ]

        try:
            job.progress = 30
            job.message = "Downloading the audio stream..."
            
            # Esegui il processo yt-dlp
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            self._procs[job.job_id] = proc
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=3600)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise TimeoutError("Download stopped: it exceeded the one-hour limit")
            finally:
                self._procs.pop(job.job_id, None)

            if job.job_id in self._cancelled:
                raise asyncio.CancelledError()
            if proc.returncode != 0:
                raise Exception(f"yt-dlp error: {stderr.decode()[:300]}")

            job.status = "tagging"
            UserDataService.update_download_status(job.job_id, job.status)
            job.progress = 70
            job.message = "Adding metadata and cover art..."

            # Applica ID3 Tags con Mutagen per renderlo perfetto per Jellyfin
            self._tag_audio(final_mp3, release)

            # Ricerca automatica Lyrics sincronizzate (LRC) e plain
            try:
                job.message = "Looking for synchronized lyrics..."
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
                job.message = "Adding it to the Jellyfin library..."
                # Refresh just the album folder and wait (up to 20 s) until Jellyfin lists the file; the
                # library monitor notification and the full scan are fallbacks for unusual library mounts.
                library_item_id = await JellyfinService.scan_file(final_mp3)
                if not library_item_id and not await JellyfinService.notify_new_file(final_mp3):
                    await JellyfinService.refresh_library()

            job.status = "completed"
            job.progress = 100
            UserDataService.update_download_status(job.job_id, job.status)
            job.message = f"Completed. Saved to {relative_path}"
            # Adding to the playlist waits for Jellyfin's scan in the background (minutes on large libraries)
            # without holding one of the two download slots.
            if job.playlist_id and settings.JELLYFIN_API_KEY:
                asyncio.create_task(self._add_to_playlist_when_ready(job, final_mp3, library_item_id))

        except asyncio.CancelledError:
            self._mark_cancelled(job)
        except Exception as e:
            if job.job_id in self._cancelled:
                self._mark_cancelled(job)
                return
            print(f"[DownloadManager] Errore: {e}")
            job.status = "failed"
            UserDataService.update_download_status(job.job_id, job.status)
            job.message = f"Download failed: {str(e)}"

    async def _add_to_playlist_when_ready(self, job: DownloadJob, path: str, item_id: Optional[str] = None):
        """Wait until Jellyfin has indexed the track (up to ~6 minutes), then add it to the job's playlist."""
        track = job.track
        name = job.playlist_name or job.playlist_id
        if item_id:
            added = await JellyfinService.add_to_playlist(job.playlist_id, item_id, user_id=job.playlist_user_id)
            job.message = f"Added to '{name}'." if added else f"Saved, but Jellyfin refused to add it to '{name}'."
            return
        job.message = f"Saved. Adding to '{name}' as soon as Jellyfin has scanned it..."
        delays = [3] * 10 + [10] * 12 + [30] * 6
        for attempt, delay in enumerate(delays):
            await asyncio.sleep(delay)
            item_id = await JellyfinService.find_track_id(track.title, track.artist, user_id=job.playlist_user_id)
            if item_id:
                added = await JellyfinService.add_to_playlist(job.playlist_id, item_id, user_id=job.playlist_user_id)
                job.message = f"Added to '{name}'." if added else f"Saved, but Jellyfin refused to add it to '{name}'."
                print(f"[DownloadManager] Playlist add for {track.title}: {'ok' if added else 'refused'} after {sum(delays[:attempt + 1])}s")
                return
            if attempt == 9:
                # Still unknown after 30 s: the targeted scan may not have matched Jellyfin's path; scan everything.
                await JellyfinService.refresh_library()
        job.message = f"Saved, but Jellyfin had not indexed it after 6 minutes; add it to '{name}' manually."
        print(f"[DownloadManager] Playlist add for {track.title}: Jellyfin never indexed {path}")

    async def _run_replace(self, job: DownloadJob):
        """Download the catalog song picked in "Change file" in the file's format, copy the old tags onto it,
        swap the audio, and finish only once Jellyfin lists the new audio (same item, so playlists keep it)."""
        old_path = job.replace_path
        item_id = job.replace_item_id or ""
        candidate = job.replace_candidate or {}
        artist, title = candidate.get("artist") or job.track.artist, candidate.get("title") or job.track.title
        tmp_dir = os.path.join(file_replace_service.DATA_DIR, "replace-tmp")
        tmp_base = os.path.join(tmp_dir, job.job_id)
        try:
            job.status = "downloading"
            UserDataService.update_download_status(job.job_id, job.status)
            job.progress = 10
            job.message = "Choosing the version with the right length..."
            source = await file_replace_service.pick_source(artist, title, int(candidate.get("duration") or 0)) \
                or f"ytsearch1:{artist} - {title} audio"
            job.progress = 25
            job.message = "Downloading the chosen song..."
            os.makedirs(tmp_dir, exist_ok=True)
            fmt = file_replace_service.output_format(old_path)
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "yt_dlp", "--extract-audio", "--audio-format", fmt, "--audio-quality", "0",
                "--no-cache-dir", "--no-playlist", "--default-search", "ytsearch", "--output", tmp_base + ".%(ext)s",
                source, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            self._procs[job.job_id] = proc
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=3600)
            finally:
                self._procs.pop(job.job_id, None)
            if job.job_id in self._cancelled:
                raise asyncio.CancelledError()
            if proc.returncode != 0:
                raise Exception(f"yt-dlp error: {stderr.decode()[:300]}")
            produced = [os.path.join(tmp_dir, name) for name in os.listdir(tmp_dir) if name.startswith(job.job_id + ".")]
            if not produced:
                raise Exception("yt-dlp produced no audio file")
            new_audio = produced[0]

            job.status = "tagging"
            UserDataService.update_download_status(job.job_id, job.status)
            job.progress = 60
            job.message = "Keeping the existing tags and cover..."
            target = old_path
            if os.path.splitext(new_audio)[1].lower() == os.path.splitext(old_path)[1].lower():
                file_replace_service.copy_tags(old_path, new_audio)
            else:
                # Lossless or unsupported format: the replacement becomes an MP3 next to it (the old file goes),
                # keeping the old file's tags and cover; without readable tags they are written from the catalog.
                target = os.path.splitext(old_path)[0] + ".mp3"
                try:
                    copied = file_replace_service.copy_tags_to_mp3(old_path, new_audio)
                except Exception as exc:
                    print(f"[Replace] Tag copy to MP3 failed for {old_path}: {exc}")
                    copied = False
                if not copied:
                    self._tag_audio(new_audio, await resolve_release(job.track))
            new_length = file_replace_service.audio_length(new_audio)
            # A new extension means a new Jellyfin item: remember the playlists holding the old one first.
            playlists = await JellyfinService.playlists_containing(item_id) if target != old_path and item_id else []
            file_replace_service.backup_and_log(old_path, item_id, source, job.owner_name or "")
            file_replace_service.install_file(new_audio, target, mode_from=old_path)
            if target != old_path:
                os.remove(old_path)
            job.file_path = target
            relative = os.path.relpath(target, os.path.realpath(settings.MUSIC_OUTPUT_DIR))

            note = ""
            if settings.JELLYFIN_API_KEY:
                job.status = "jellyfin_sync"
                UserDataService.update_download_status(job.job_id, job.status)
                job.progress = 80
                job.message = "Updating the song in Jellyfin..."
                if target == old_path:
                    if not await JellyfinService.refresh_replaced_item(item_id, new_length):
                        note = " Jellyfin is still reading the new audio; it shows up in a few minutes."
                else:
                    new_id = await JellyfinService.scan_file(target, wait=60, changed=True)
                    if not new_id:
                        note = " Jellyfin has not listed the new file yet; it shows up after its next scan."
                    else:
                        job.track.jellyfin_id = new_id
                        for playlist_id, user_id, entries in playlists:
                            await JellyfinService.swap_playlist_item(playlist_id, entries, item_id, new_id, user_id)
            job.status = "completed"
            job.progress = 100
            job.message = f"File replaced: {relative}.{note}"
            UserDataService.update_download_status(job.job_id, job.status)
        except asyncio.CancelledError:
            self._mark_cancelled(job)
        except Exception as exc:
            if job.job_id in self._cancelled:
                self._mark_cancelled(job)
                return
            job.status = "failed"
            job.message = f"Replacement failed: {exc}"
            UserDataService.update_download_status(job.job_id, job.status)
        finally:
            for name in (os.listdir(tmp_dir) if os.path.isdir(tmp_dir) else []):
                if name.startswith(job.job_id + "."):
                    os.remove(os.path.join(tmp_dir, name))

    def _tag_audio(self, mp3_path: str, track: ReleaseInfo):
        """Write ID3 metadata and the high-resolution cover into an MP3; other formats get text tags only."""
        if not os.path.exists(mp3_path):
            return
        if not mp3_path.lower().endswith(".mp3"):
            self._tag_other(mp3_path, track)
            return

        try:
            try:
                audio = MP3(mp3_path, ID3=ID3)
            except ID3NoHeaderError:
                audio = MP3(mp3_path)
                audio.add_tags()

            audio.tags.add(TIT2(encoding=3, text=track.title))
            # One TPE1 value per artist, so Jellyfin lists the track under the main artist and every guest.
            audio.tags.add(TPE1(encoding=3, text=track.artists or [track.artist]))
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

    @staticmethod
    def _tag_other(path: str, track: ReleaseInfo):
        """Write the text tags of FLAC, M4A, and Ogg files through mutagen's format-neutral keys."""
        try:
            audio = mutagen.File(path, easy=True)
            if audio is None:
                return
            if audio.tags is None:
                audio.add_tags()
            values = {
                "title": track.title, "artist": track.artists or [track.artist], "album": track.album,
                "albumartist": track.album_artist, "tracknumber": track.track_number,
                "discnumber": track.disc_number, "date": track.year, "isrc": track.isrc,
            }
            for key, value in values.items():
                if not value:
                    continue
                try:
                    audio[key] = [str(v) for v in value] if isinstance(value, list) else str(value)
                except (KeyError, ValueError):
                    pass  # The format has no field for this key (e.g. ISRC in M4A).
            audio.save()
        except Exception as e:
            print(f"[Tagging] Could not write tags to {path}: {e}")


def _free_stem(folder: str, stem: str) -> str:
    """First of `stem`, `stem (2)`, `stem (3)`, ... that no audio or lyrics file in the folder uses."""
    try:
        taken = {os.path.splitext(name)[0].casefold() for name in os.listdir(folder)}
    except OSError:
        taken = set()
    candidate, number = stem, 2
    while candidate.casefold() in taken:
        candidate, number = f"{stem} ({number})", number + 1
    return candidate


download_manager = DownloadManager()
