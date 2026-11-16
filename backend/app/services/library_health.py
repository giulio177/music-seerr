"""Library health check: flag files whose name, folder, or tags disagree, and fix them on request.

The scan only reads tags (no network) and never changes a file. Every fix is an explicit
administrator action, applied to one file and logged to `library-health.jsonl` in the data folder.
"""

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import mutagen

from app.config import settings
from app.services.library_layout import (
    AUDIO_EXTENSIONS,
    ReleaseInfo,
    album_directory,
    normalize_key,
    sanitize_component,
    split_artists,
    canonical_artist,
    title_key,
    track_stem,
    _year,
)

SIDE_FILE_EXTENSIONS = (".lrc",)
VARIOUS_ARTISTS = {"variousartists", "various", "va", "verschiedeneinterpreten", "artistivari"}
MAX_KEPT_JOBS = 5

# Issue codes. A track artist that differs from the album artist alone is normal (guests, soundtracks,
# former band names), so it only adds the "use_artist" fix to a title mismatch instead of being an issue.
TITLE_MISMATCH = "title_mismatch"        # file name title differs from the title tag
FOLDER_MISMATCH = "folder_mismatch"      # artist folder differs from the album artist tag
TRACK_COLLISION = "track_collision"      # two different titles share a disc and track number
NUMBER_PREFIX = re.compile(r"^\d{1,4}(?:[-.]\d{1,3})?\.?\s+")
NAME_TRACK_NUMBER = re.compile(r" - (\d{1,3}) - ")
CREDIT_SEPARATOR = re.compile(r"\s*(?:&|,|;|/|\+|\band\b|\bx\b)\s*", re.IGNORECASE)


def music_root() -> str:
    return os.path.realpath(settings.MUSIC_OUTPUT_DIR)


def _first(value) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").strip()


def _number(value) -> Optional[int]:
    text = _first(value).split("/")[0].strip()
    return int(text) if text.isdigit() and int(text) > 0 else None


def read_tags(path: str) -> Optional[dict]:
    """Text tags through mutagen's format-neutral keys; None when the file has no readable tags."""
    try:
        audio = mutagen.File(path, easy=True)
    except Exception:
        return None
    if audio is None or not audio.tags:
        return None
    tags = audio.tags
    artists = []
    for value in tags.get("artist") or []:
        artists += split_artists(str(value))
    return {
        "title": _first(tags.get("title")),
        "artists": artists,
        "album_artist": _first(tags.get("albumartist")),
        "album": _first(tags.get("album")),
        "year": _year(_first(tags.get("date"))),
        "track": _number(tags.get("tracknumber")),
        "disc": _number(tags.get("discnumber")),
        "duration": int(getattr(audio.info, "length", 0) or 0),
    }


def name_matches_title(stem: str, title: str) -> bool:
    """True when the file name ends with the title, as in `Artist - Album - NN - Title` or plain `Title`."""
    wanted = title_key(sanitize_component(title))
    if not wanted:
        return True
    parts = stem.split(" - ")
    candidates = [" - ".join(parts[index:]) for index in range(len(parts))] + [NUMBER_PREFIX.sub("", stem)]
    return any(title_key(candidate) == wanted for candidate in candidates)


def _artist_keys(names: List[str]) -> set:
    return {normalize_key(canonical_artist(name)) for name in names if name}


def _issue_id(relative: str, code: str) -> str:
    return hashlib.sha1(f"{code}\0{relative}".encode("utf-8")).hexdigest()[:16]


def _credit_keys(credit: str) -> set:
    """Keys of a credit and each name in it ("Max Pezzali & 883" -> whole, "maxpezzali", "883")."""
    names = [credit] + split_artists(credit) + CREDIT_SEPARATOR.split(credit)
    return {normalize_key(sanitize_component(canonical_artist(name))) for name in names if name.strip()} - {""}


def check_file(root: str, path: str, tags: dict) -> List[dict]:
    """Per-file checks; the cross-file track collision check runs per folder in `scan_folder`.
    Loose files in the library root are outside the layout and skipped."""
    relative = os.path.relpath(path, root)
    parts = relative.split(os.sep)
    if len(parts) < 2:
        return []
    stem = os.path.splitext(os.path.basename(path))[0]
    issues = []
    album_artist = tags["album_artist"]
    if tags["title"] and not name_matches_title(stem, tags["title"]):
        fixes = ["rename"]
        if tags["artists"] and album_artist and not _credit_keys(album_artist) & _artist_keys(tags["artists"]):
            fixes.append("use_artist")
        issues.append({"code": TITLE_MISMATCH, "expected": tags["title"], "fixes": fixes + ["delete"]})
    if album_artist and len(parts) >= 3 and normalize_key(album_artist) not in VARIOUS_ARTISTS \
            and normalize_key(parts[0]) not in VARIOUS_ARTISTS and normalize_key(parts[0]) not in _credit_keys(album_artist):
        issues.append({"code": FOLDER_MISMATCH, "expected": canonical_artist(album_artist), "fixes": ["move", "delete"]})
    return issues


def scan_folder(root: str, folder: str, names: List[str]) -> List[dict]:
    """Every issue for the audio files of one folder."""
    found = []
    slots: Dict[tuple, List[tuple]] = {}
    for name in sorted(names):
        if not name.lower().endswith(AUDIO_EXTENSIONS) or name.startswith("._"):
            continue
        path = os.path.join(folder, name)
        tags = read_tags(path)
        if not tags:
            continue
        for issue in check_file(root, path, tags):
            found.append(_issue(root, path, tags, issue))
        # The number in a Lidarr-style name wins over the tag, which is often wrong on whole albums.
        named = NAME_TRACK_NUMBER.search(os.path.splitext(name)[0])
        number = int(named.group(1)) if named else tags["track"]
        if number and tags["title"] and len(os.path.relpath(folder, root).split(os.sep)) >= 2:
            slots.setdefault((tags["disc"] or 1, number), []).append((path, tags))
    for (disc, track), group in slots.items():
        if len({title_key(tags["title"]) for _, tags in group}) < 2:
            continue
        for path, tags in group:
            others = [os.path.basename(other) for other, _ in group if other != path]
            found.append(_issue(root, path, tags, {"code": TRACK_COLLISION, "expected": ", ".join(others), "fixes": ["delete"]}))
    return found


def _issue(root: str, path: str, tags: dict, issue: dict) -> dict:
    relative = os.path.relpath(path, root)
    return {
        "id": _issue_id(relative, issue["code"]),
        "code": issue["code"],
        "path": relative,
        "expected": issue["expected"],
        "title": tags["title"],
        "artists": tags["artists"],
        "album_artist": tags["album_artist"],
        "album": tags["album"],
        "duration": tags["duration"],
        "fixes": issue["fixes"],
    }


def _walk(root: str):
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if not name.startswith("."))
        if any(name.lower().endswith(AUDIO_EXTENSIONS) for name in files):
            yield current, files


def release_from_tags(tags: dict, album_artist: Optional[str] = None) -> ReleaseInfo:
    artists = tags["artists"] or ["Unknown"]
    return ReleaseInfo(
        title=tags["title"],
        artist=artists[0],
        artists=artists,
        album_artist=album_artist or tags["album_artist"] or artists[0],
        album=tags["album"] or tags["title"],
        year=tags["year"],
        track_number=tags["track"],
        disc_number=tags["disc"],
        matched=True,
    )


def _move_with_side_files(source: str, target: str) -> dict:
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.move(source, target)
    record = {"source": source, "target": target, "side_files": []}
    source_stem, target_stem = os.path.splitext(source)[0], os.path.splitext(target)[0]
    for ext in SIDE_FILE_EXTENSIONS:
        if os.path.exists(source_stem + ext) and not os.path.exists(target_stem + ext):
            shutil.move(source_stem + ext, target_stem + ext)
            record["side_files"].append([source_stem + ext, target_stem + ext])
    return record


def _remove_empty_dirs(folder: str, root: str):
    """Drop folders left with only artwork or metadata after a move or delete."""
    disposable = (".nfo", ".jpg", ".jpeg", ".png")
    while folder != root and os.path.commonpath([root, folder]) == root:
        try:
            names = os.listdir(folder)
        except OSError:
            return
        if any(os.path.isdir(os.path.join(folder, name)) for name in names) \
                or not all(name.lower().endswith(disposable) for name in names):
            return
        for name in names:
            os.remove(os.path.join(folder, name))
        os.rmdir(folder)
        folder = os.path.dirname(folder)


@dataclass
class HealthJob:
    id: str
    status: str = "scanning"
    total: int = 0
    processed: int = 0
    issues: List[dict] = field(default_factory=list)
    error: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self, ignored: set) -> dict:
        visible = [issue for issue in self.issues if issue["id"] not in ignored]
        return {
            "id": self.id, "status": self.status, "total": self.total, "processed": self.processed,
            "error": self.error, "issues": visible if self.status != "scanning" else [],
            "ignored": len(self.issues) - len(visible),
        }


class LibraryHealth:
    jobs: Dict[str, HealthJob] = {}

    @classmethod
    def start_scan(cls) -> HealthJob:
        from app.services.library_import import LibraryImporter
        if LibraryImporter.busy() or any(job.status == "scanning" for job in cls.jobs.values()):
            raise ValueError("busy")
        job = HealthJob(id=uuid.uuid4().hex)
        cls.jobs[job.id] = job
        for old in sorted(cls.jobs.values(), key=lambda item: item.created_at)[:-MAX_KEPT_JOBS]:
            cls.jobs.pop(old.id, None)
        asyncio.create_task(cls._scan(job))
        return job

    @classmethod
    async def _scan(cls, job: HealthJob):
        from app.services.library_import import LibraryImporter
        try:
            async with LibraryImporter._busy:
                root = music_root()
                folders = await asyncio.to_thread(lambda: list(_walk(root)))
                job.total = len(folders)
                for folder, names in folders:
                    job.issues += await asyncio.to_thread(scan_folder, root, folder, names)
                    job.processed += 1
            job.status = "ready"
        except Exception as exc:
            print(f"[LibraryHealth] Scan failed: {exc}")
            job.status, job.error = "failed", "scan_failed"

    @classmethod
    def find_issue(cls, job_id: str, issue_id: str) -> tuple:
        job = cls.jobs.get(job_id)
        if not job or job.status != "ready":
            raise ValueError("job_not_found")
        issue = next((item for item in job.issues if item["id"] == issue_id), None)
        if not issue:
            raise ValueError("issue_not_found")
        return job, issue

    @classmethod
    async def fix(cls, job_id: str, issue_id: str, action: str, user: dict) -> dict:
        """Apply one fix to the file behind an issue; every issue of that file leaves the list."""
        from app.services.library_import import LibraryImporter
        job, issue = cls.find_issue(job_id, issue_id)
        if action not in issue["fixes"]:
            raise ValueError("fix_not_allowed")
        if LibraryImporter.busy():
            raise ValueError("busy")
        async with LibraryImporter._busy:
            record = await asyncio.to_thread(cls._apply_fix, issue, action)
        record.update(action=action, code=issue["code"], user=user.get("name", ""), at=time.time())
        cls._log(record)
        job.issues = [item for item in job.issues if item["path"] != issue["path"]]
        if settings.JELLYFIN_API_KEY:
            from app.services.jellyfin_service import JellyfinService
            await JellyfinService.refresh_library()
        return {"path": os.path.relpath(record.get("target") or record["source"], music_root()), "action": action}

    @staticmethod
    def _apply_fix(issue: dict, action: str) -> dict:
        root = music_root()
        source = os.path.realpath(os.path.join(root, issue["path"]))
        if os.path.commonpath([root, source]) != root or not os.path.isfile(source):
            raise ValueError("file_not_found")
        tags = read_tags(source)
        if not tags:
            raise ValueError("file_not_found")
        folder = os.path.dirname(source)

        if action == "delete":
            removed = [source]
            os.remove(source)
            stem = os.path.splitext(source)[0]
            for ext in SIDE_FILE_EXTENSIONS:
                if os.path.exists(stem + ext):
                    os.remove(stem + ext)
                    removed.append(stem + ext)
            _remove_empty_dirs(folder, root)
            return {"source": source, "deleted": removed}

        if not tags["title"]:
            raise ValueError("fix_not_allowed")
        album_artist = tags["artists"][0] if action == "use_artist" and tags["artists"] else None
        release = release_from_tags(tags, album_artist)
        if action == "use_artist":
            _set_album_artist(source, release.album_artist)
        # "rename" keeps the folder (e.g. a disc subfolder); "move" and "use_artist" rebuild the whole path.
        target_dir = folder if action == "rename" else album_directory(root, release)
        target = os.path.join(target_dir, track_stem(release) + os.path.splitext(source)[1].lower())
        if os.path.commonpath([root, os.path.realpath(os.path.dirname(target))]) != root:
            raise ValueError("file_not_found")
        if os.path.realpath(target) == source:
            return {"source": source, "target": target, "side_files": []}
        if os.path.exists(target):
            raise ValueError("target_exists")
        record = _move_with_side_files(source, target)
        _remove_empty_dirs(folder, root)
        return record

    @staticmethod
    def _log(record: dict):
        data_dir = os.path.dirname(os.environ.get("MUSIC_SEERR_CONFIG") or "/app/data/config.json")
        if not os.path.isdir(data_dir):
            return
        with open(os.path.join(data_dir, "library-health.jsonl"), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def _set_album_artist(path: str, album_artist: str):
    """Change only the album artist tag, keeping every other tag and the cover."""
    if path.lower().endswith(".mp3"):
        from mutagen.id3 import ID3, TPE2
        tags = ID3(path)
        tags.add(TPE2(encoding=3, text=album_artist))
        tags.save()
        return
    audio = mutagen.File(path, easy=True)
    audio["albumartist"] = album_artist
    audio.save()
