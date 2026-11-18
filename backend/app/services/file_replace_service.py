"""Replace the audio of a library file whose tags are right but whose recording is wrong.

The administrator picks a YouTube result (previewed through the backend, so the preview is exactly the audio
that will be downloaded); the download keeps the file's path, format, tags, cover, and lyrics sidecar.
The previous audio is kept in `data/replaced-files/` for 30 days and every replacement is logged.
"""
import asyncio
import json
import os
import re
import shutil
import sys
import time
from typing import List, Optional

import mutagen
from mutagen.flac import FLAC
from mutagen.id3 import ID3, ID3NoHeaderError
from mutagen.mp4 import MP4

from app.config import settings
from app.services.jellyfin_service import JellyfinService

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
# yt-dlp --audio-format value per library extension; other formats are replaced by an MP3 next to them.
AUDIO_FORMATS = {".mp3": "mp3", ".m4a": "m4a", ".flac": "flac", ".opus": "opus", ".ogg": "vorbis", ".wav": "wav"}
BACKUP_DAYS = 30
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data")

_search_cache: dict = {}
_url_cache: dict = {}


async def _yt_dlp(*args: str, timeout: int = 60) -> str:
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-cache-dir", *args,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("yt-dlp timed out")
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode(errors="replace")[:300])
    return stdout.decode(errors="replace")


async def search_candidates(query: str, limit: int = 12) -> List[dict]:
    """YouTube results for a query (metadata only, cached ten minutes)."""
    key = (query.strip().casefold(), limit)
    hit = _search_cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    raw = await _yt_dlp("--flat-playlist", "--dump-single-json", f"ytsearch{limit}:{query}", timeout=45)
    results = []
    for entry in (json.loads(raw).get("entries") or []):
        video_id = entry.get("id") or ""
        if not VIDEO_ID.match(video_id):
            continue
        results.append({
            "id": video_id,
            "title": entry.get("title") or "",
            "channel": entry.get("channel") or entry.get("uploader") or "",
            "duration": int(entry.get("duration") or 0),
            "views": int(entry.get("view_count") or 0),
            "verified": bool(entry.get("channel_is_verified")),
            "thumbnail": f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
            "url": f"https://www.youtube.com/watch?v={video_id}",
        })
    if len(_search_cache) > 200:
        _search_cache.clear()
    _search_cache[key] = (time.time() + 600, results)
    return results


async def audio_url(video_id: str) -> str:
    """Direct audio stream URL of a video (bound to this server's IP, so the backend proxies it)."""
    if not VIDEO_ID.match(video_id):
        raise ValueError("invalid_video")
    hit = _url_cache.get(video_id)
    if hit and hit[0] > time.time():
        return hit[1]
    url = (await _yt_dlp("-f", "bestaudio[ext=m4a]/bestaudio", "-g", "--no-playlist",
                         f"https://www.youtube.com/watch?v={video_id}", timeout=45)).strip().splitlines()[0]
    if len(_url_cache) > 200:
        _url_cache.clear()
    _url_cache[video_id] = (time.time() + 1800, url)
    return url


async def local_path_for(jellyfin_path: str) -> Optional[str]:
    """Map a path reported by Jellyfin to the file in the music root (Jellyfin may mount it elsewhere)."""
    music_root = os.path.realpath(settings.MUSIC_OUTPUT_DIR)
    jellyfin_path = (jellyfin_path or "").replace("\\", "/")
    candidates = []
    for root in await JellyfinService.get_music_library_paths():
        root = root.rstrip("/").replace("\\", "/")
        if root and jellyfin_path.startswith(root + "/"):
            candidates.append(jellyfin_path[len(root) + 1:])
    # Fallback: the shortest trailing part of the path that exists in the music root.
    parts = [part for part in jellyfin_path.split("/") if part]
    candidates += ["/".join(parts[i:]) for i in range(len(parts))]
    for relative in candidates:
        path = os.path.realpath(os.path.join(music_root, relative))
        if path.startswith(music_root + os.sep) and os.path.isfile(path):
            return path
    return None


def output_format(path: str) -> str:
    return AUDIO_FORMATS.get(os.path.splitext(path)[1].lower(), "mp3")


def copy_tags(source: str, target: str):
    """Copy every tag (text, cover art, embedded lyrics) from the old file to the new one of the same format."""
    ext = os.path.splitext(source)[1].lower()
    if ext == ".mp3":
        try:
            ID3(source).save(target)
        except ID3NoHeaderError:
            pass
        return
    if ext == ".flac":
        old, new = FLAC(source), FLAC(target)
        new.delete()
        new.clear_pictures()
        for key, values in (old.tags or {}).items():
            new[key] = values
        for picture in old.pictures:
            new.add_picture(picture)
        new.save()
        return
    if ext == ".m4a":
        old, new = MP4(source), MP4(target)
        if new.tags is None:
            new.add_tags()
        new.tags.clear()
        new.tags.update(dict(old.tags or {}))
        new.save()
        return
    old, new = mutagen.File(source), mutagen.File(target)
    if old is not None and new is not None and old.tags is not None:
        if new.tags is None:
            new.add_tags()
        for key, value in old.tags.items():
            new.tags[key] = value
        new.save()


def backup_and_log(old_path: str, item_id: str, video_id: str, user: str, extra: Optional[dict] = None) -> str:
    """Keep the previous audio (30 days) and append the replacement to `replaced-files.jsonl`."""
    backup_dir = os.path.join(DATA_DIR, "replaced-files")
    os.makedirs(backup_dir, exist_ok=True)
    cutoff = time.time() - BACKUP_DAYS * 86400
    for name in os.listdir(backup_dir):
        full = os.path.join(backup_dir, name)
        if os.path.isfile(full) and os.path.getmtime(full) < cutoff:
            os.remove(full)
    backup = os.path.join(backup_dir, f"{time.strftime('%Y%m%d-%H%M%S')}-{os.path.basename(old_path)}")
    shutil.copy2(old_path, backup)
    with open(os.path.join(DATA_DIR, "replaced-files.jsonl"), "a", encoding="utf-8") as log:
        log.write(json.dumps({"at": time.time(), "path": old_path, "backup": backup, "jellyfin_item": item_id,
                              "source": f"https://www.youtube.com/watch?v={video_id}" if video_id else None, "user": user, **(extra or {})}, ensure_ascii=False) + "\n")
    return backup


# ---------------------------------------------------------------- source choice for new downloads

def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", (text or "").casefold())


async def pick_source(artist: str, title: str, duration: int) -> Optional[str]:
    """Best YouTube video for a catalog track: same length (within 2 s) wins, auto-generated "- Topic" uploads
    (the plain studio audio) first; otherwise the closest length within 10 s. None keeps the old first-hit search."""
    if not duration:
        return None
    try:
        results = await search_candidates(f"{artist} - {title}", 10)
    except (RuntimeError, ValueError):
        return None
    key = _norm(re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", title))[:24]

    def score(item):
        diff = abs((item["duration"] or 0) - duration)
        named = key and key in _norm(item["title"])
        topic = item["channel"].endswith(" - Topic")
        return (diff > 2, not named, not topic, diff)

    candidates = [item for item in results if item["duration"] and abs(item["duration"] - duration) <= 10]
    if not candidates:
        return None
    best = min(candidates, key=score)
    return best["url"]


# ---------------------------------------------------------------- trimming ("Edit file")

async def waveform(path: str, buckets: int = 720) -> dict:
    """Peak envelope of the file (mono, 4 kHz) for the editor."""
    proc = await asyncio.create_subprocess_exec("ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", "4000", "-f", "s16le", "-",
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    raw, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    samples = memoryview(raw).cast("h") if raw else memoryview(b"").cast("h")
    count = len(samples)
    duration = count / 4000
    size = max(1, count // buckets)
    peaks = []
    for start in range(0, count, size):
        chunk = samples[start:start + size]
        peaks.append(round(max(abs(min(chunk)), abs(max(chunk))) / 32768, 3) if len(chunk) else 0)
    return {"duration": round(duration, 2), "peaks": peaks[:buckets]}


def _shift_lrc(lrc_path: str, offset: float, end: float):
    """Move synced lyrics by -offset seconds and drop lines outside the kept range."""
    if not os.path.isfile(lrc_path):
        return
    out = []
    stamp = re.compile(r"\[(\d+):(\d{2}(?:\.\d+)?)\]")
    with open(lrc_path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            times = stamp.findall(line)
            if not times:
                out.append(line)
                continue
            text = stamp.sub("", line)
            kept = []
            for minutes, seconds in times:
                value = int(minutes) * 60 + float(seconds) - offset
                if 0 <= value <= end - offset + 1:
                    kept.append(f"[{int(value // 60):02d}:{value % 60:05.2f}]")
            if kept:
                out.append("".join(kept) + text)
    with open(lrc_path, "w", encoding="utf-8") as handle:
        handle.writelines(out)


async def trim(path: str, start: float, end: float, item_id: str, user: str) -> str:
    """Cut the file to [start, end] without re-encoding (frame-accurate), keeping tags and cover; shifts the .lrc."""
    ext = os.path.splitext(path)[1].lower()
    tmp_dir = os.path.join(DATA_DIR, "replace-tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    tmp = os.path.join(tmp_dir, f"trim-{item_id}{ext}")
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", path,
           "-map", "0", "-c", "copy", "-map_metadata", "0"]
    if ext == ".mp3":
        cmd += ["-id3v2_version", "3"]
    proc = await asyncio.create_subprocess_exec(*cmd, tmp, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
    if proc.returncode != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) < 1024:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise RuntimeError(stderr.decode(errors="replace")[:300] or "ffmpeg failed")
    backup = backup_and_log(path, item_id, "", user, {"trim": [round(start, 3), round(end, 3)]})
    shutil.move(tmp, path)
    _shift_lrc(os.path.splitext(path)[0] + ".lrc", start, end)
    return backup
