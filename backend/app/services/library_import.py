"""Import audio files from a folder into the Lidarr-style library layout.

Sources are either a folder on the server (inside the music root or the optional import mount)
or files uploaded from the browser into a hidden staging folder inside the music root.
Each import runs as a two-step job: scan (plan every move) and apply (move, retag, refresh Jellyfin).
"""

import asyncio
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
from app.models.schemas import TrackItem
from app.services.library_layout import (
    AUDIO_EXTENSIONS,
    ReleaseInfo,
    album_directory,
    audio_quality,
    find_existing_tracks,
    normalize_key,
    resolve_release,
    track_stem,
)

SIDE_FILE_EXTENSIONS = (".lrc",)
DISPOSABLE_EXTENSIONS = (".nfo", ".jpg", ".jpeg", ".png", ".txt", ".cue", ".log", ".db", ".ini", ".ds_store")
STAGING_DIRNAME = ".musicseerr-import"
UPLOAD_EXTENSIONS = AUDIO_EXTENSIONS + SIDE_FILE_EXTENSIONS
MAX_UPLOAD_FILE_BYTES = 200 * 1024 * 1024
MAX_UPLOAD_SESSION_FILES = 5000
MAX_KEPT_JOBS = 10


def music_root() -> str:
    return os.path.realpath(settings.MUSIC_OUTPUT_DIR)


def import_root() -> Optional[str]:
    """Optional extra mount for folders outside the library (e.g. a downloads folder)."""
    path = os.environ.get("MUSIC_IMPORT_DIR", "/import")
    real = os.path.realpath(path)
    return real if os.path.isdir(real) else None


def staging_root() -> str:
    return os.path.join(music_root(), STAGING_DIRNAME)


def browse_roots() -> List[dict]:
    roots = [{"id": "library", "path": music_root()}]
    extra = import_root()
    if extra and extra != music_root():
        roots.append({"id": "import", "path": extra})
    return roots


def _inside(path: str, root: str) -> bool:
    return os.path.commonpath([root, path]) == root


def resolve_source(path: str) -> str:
    """Return the real path when it is a folder inside an allowed root, else raise ValueError."""
    real = os.path.realpath(path or "")
    if not os.path.isdir(real):
        raise ValueError("folder_not_found")
    if not any(_inside(real, root["path"]) for root in browse_roots()):
        raise ValueError("folder_not_allowed")
    if _inside(real, staging_root()):
        raise ValueError("folder_not_allowed")
    return real


def _visible_dirs(names: List[str]) -> List[str]:
    return [name for name in names if not name.startswith(".")]


def list_folder(path: Optional[str]) -> dict:
    """List subfolders of an allowed folder; with no path, list the browse roots."""
    if not path:
        return {"path": "", "parent": None, "folders": [
            {"name": root["id"], "path": root["path"], "root": root["id"]} for root in browse_roots()
        ], "audio_files": 0}
    real = resolve_source(path)
    folders, audio_files = [], 0
    with os.scandir(real) as entries:
        for entry in sorted(entries, key=lambda item: item.name.casefold()):
            if entry.name.startswith("."):
                continue
            if entry.is_dir(follow_symlinks=False):
                folders.append({"name": entry.name, "path": entry.path})
            elif entry.name.lower().endswith(AUDIO_EXTENSIONS):
                audio_files += 1
    parent = os.path.dirname(real)
    is_root = any(real == root["path"] for root in browse_roots())
    return {"path": real, "parent": None if is_root else parent, "folders": folders, "audio_files": audio_files}


def _audio_files(source_root: str) -> List[str]:
    found = []
    for root, dirs, files in os.walk(source_root):
        dirs[:] = sorted(_visible_dirs(dirs))
        found += [os.path.join(root, name) for name in files if name.lower().endswith(AUDIO_EXTENSIONS)]
    return sorted(found)


def read_track(path: str, source_root: str) -> TrackItem:
    """Build a lookup query from the file's tags, falling back to folder and file names."""
    tags, duration = {}, 0
    try:
        audio = mutagen.File(path, easy=True)
        if audio is not None:
            tags = {key: (value[0] if isinstance(value, list) and value else value) for key, value in (audio.tags or {}).items()}
            duration = int(getattr(audio.info, "length", 0) or 0)
    except Exception as exc:
        print(f"[LibraryImport] Could not read tags of {path}: {exc}")
    parts = os.path.relpath(path, source_root).split(os.sep)
    stem = os.path.splitext(parts[-1])[0]
    title = str(tags.get("title") or "")
    artist = str(tags.get("artist") or tags.get("albumartist") or "")
    if not title or not artist:
        # "Artist - Title" or "NN - Title" file names are common for untagged downloads.
        pieces = [piece.strip() for piece in re.split(r"\s+-\s+", stem) if piece.strip()]
        pieces = [piece for piece in pieces if not piece.isdigit()]
        if not title:
            title = pieces[-1] if pieces else stem
        if not artist:
            artist = pieces[0] if len(pieces) > 1 else (parts[0] if len(parts) > 2 else "Unknown")
    return TrackItem(
        id=f"local:{len(path)}",
        title=title[:300],
        artist=artist[:200],
        album=str(tags.get("album") or (parts[-2] if len(parts) > 1 else title))[:200],
        duration=min(duration, 86400),
    )


@dataclass
class ImportJob:
    id: str
    source: str
    label: str
    upload_session: Optional[str] = None
    status: str = "scanning"
    total: int = 0
    processed: int = 0
    entries: List[dict] = field(default_factory=list)
    moved: int = 0
    deleted: int = 0
    error: str = ""
    manifest: str = ""
    created_at: float = field(default_factory=time.time)

    def summary(self) -> dict:
        counts: Dict[str, int] = {}
        for entry in self.entries:
            counts[entry["action"]] = counts.get(entry["action"], 0) + 1
        return counts

    def to_dict(self, include_entries: bool = True) -> dict:
        root = music_root()
        data = {
            "id": self.id,
            "label": self.label,
            "status": self.status,
            "total": self.total,
            "processed": self.processed,
            "summary": self.summary(),
            "unmatched": sum(not entry["matched"] for entry in self.entries),
            "moved": self.moved,
            "deleted": self.deleted,
            "error": self.error,
            "is_upload": bool(self.upload_session),
        }
        if include_entries:
            data["entries"] = [{
                "source": os.path.relpath(entry["source"], self.source),
                "target": os.path.relpath(entry["target"], root),
                "action": entry["action"],
                "matched": entry["matched"],
                "quality": entry.get("quality", ""),
                "kept": os.path.relpath(entry["kept"], root) if entry.get("kept") else "",
                "kept_quality": entry.get("kept_quality", ""),
                "replaces": [os.path.relpath(path, root) for path in entry.get("replaces", [])],
            } for entry in self.entries]
        return data


class LibraryImporter:
    jobs: Dict[str, ImportJob] = {}
    uploads: Dict[str, dict] = {}
    _busy = asyncio.Lock()

    # ---- uploads from the browser -------------------------------------------------

    @classmethod
    def create_upload_session(cls) -> str:
        session_id = uuid.uuid4().hex
        path = os.path.join(staging_root(), session_id)
        os.makedirs(path, exist_ok=True)
        cls.uploads[session_id] = {"path": path, "files": 0, "created_at": time.time()}
        return session_id

    @classmethod
    def upload_target(cls, session_id: str, relative_path: str) -> str:
        session = cls.uploads.get(session_id)
        if not session:
            raise ValueError("upload_not_found")
        if session["files"] >= MAX_UPLOAD_SESSION_FILES:
            raise ValueError("too_many_files")
        parts = [part for part in re.split(r"[\\/]+", relative_path or "") if part not in ("", ".", "..")]
        parts = [re.sub(r'[\x00-\x1f*?:"<>|]', "", part).strip()[:150] for part in parts[-4:]]
        parts = [part for part in parts if part and not part.startswith(".")]
        if not parts or not parts[-1].lower().endswith(UPLOAD_EXTENSIONS):
            raise ValueError("unsupported_file")
        target = os.path.realpath(os.path.join(session["path"], *parts))
        if not _inside(target, session["path"]) or target == session["path"]:
            raise ValueError("unsupported_file")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        session["files"] += 1
        return target

    @classmethod
    def discard_upload(cls, session_id: Optional[str]):
        session = cls.uploads.pop(session_id, None) if session_id else None
        if session:
            shutil.rmtree(session["path"], ignore_errors=True)

    @classmethod
    def cleanup_stale_uploads(cls):
        """Remove staging folders left by interrupted uploads (run at startup)."""
        root = staging_root()
        if not os.path.isdir(root):
            return
        active = {session["path"] for session in cls.uploads.values()}
        for entry in os.scandir(root):
            if entry.path in active:
                continue
            shutil.rmtree(entry.path, ignore_errors=True)
        try:
            os.rmdir(root)
        except OSError:
            pass

    # ---- scan and apply -----------------------------------------------------------

    @classmethod
    def _remember(cls, job: ImportJob):
        cls.jobs[job.id] = job
        finished = [item for item in cls.jobs.values() if item.status in ("ready", "done", "failed")]
        for old in sorted(finished, key=lambda item: item.created_at)[:-MAX_KEPT_JOBS]:
            cls.jobs.pop(old.id, None)
            if old.status != "ready":
                continue
            cls.discard_upload(old.upload_session)

    @classmethod
    def busy(cls) -> bool:
        return any(job.status in ("scanning", "applying") for job in cls.jobs.values())

    @classmethod
    def start_scan(cls, path: Optional[str] = None, upload_session: Optional[str] = None) -> ImportJob:
        if cls.busy():
            raise ValueError("busy")
        if upload_session:
            session = cls.uploads.get(upload_session)
            if not session:
                raise ValueError("upload_not_found")
            source, label = session["path"], "upload"
        else:
            source = resolve_source(path)
            label = source
        job = ImportJob(id=uuid.uuid4().hex, source=source, label=label, upload_session=upload_session)
        cls._remember(job)
        asyncio.create_task(cls._scan(job))
        return job

    @classmethod
    async def plan(cls, job: ImportJob, log: bool = False):
        """Plan every file, then settle each track slot so only its best-quality copy is kept."""
        root = music_root()
        files = await asyncio.to_thread(_audio_files, job.source)
        job.total = len(files)
        slots: Dict[tuple, List[dict]] = {}
        for path in files:
            track = await asyncio.to_thread(read_track, path, job.source)
            release = await resolve_release(track)
            dest_dir = album_directory(root, release)
            entry = {
                "source": path,
                "target": os.path.join(dest_dir, track_stem(release) + os.path.splitext(path)[1].lower()),
                "action": "move",
                "matched": release.matched,
                "existing": await asyncio.to_thread(find_existing_tracks, dest_dir, release),
                "release": {
                    "title": release.title, "artist": release.artist, "album_artist": release.album_artist,
                    "album": release.album, "year": release.year, "track_number": release.track_number,
                    "disc_number": release.disc_number, "isrc": release.isrc, "cover_url": release.cover_url,
                },
            }
            job.entries.append(entry)
            slots.setdefault((dest_dir, normalize_key(release.title) or path), []).append(entry)
            job.processed += 1

        for group in slots.values():
            await asyncio.to_thread(_settle_slot, group)
        if log:
            for entry in job.entries:
                print(f"{entry['action']:9} {'' if entry['matched'] else '(no match) '}"
                      f"{os.path.relpath(entry['source'], root)} -> {os.path.relpath(entry['target'], root)}"
                      f"{'  [keeps ' + os.path.relpath(entry['kept'], root) + ']' if entry.get('kept') else ''}")

    @classmethod
    async def _scan(cls, job: ImportJob):
        try:
            async with cls._busy:
                await cls.plan(job)
            job.status = "ready"
        except Exception as exc:
            print(f"[LibraryImport] Scan failed for {job.source}: {exc}")
            job.status, job.error = "failed", "scan_failed"

    @classmethod
    def start_apply(cls, job_id: str, delete_duplicates: bool = False) -> ImportJob:
        job = cls.jobs.get(job_id)
        if not job:
            raise ValueError("job_not_found")
        if job.status != "ready":
            raise ValueError("job_not_ready")
        if cls.busy():
            raise ValueError("busy")
        job.status = "applying"
        job.processed = 0
        asyncio.create_task(cls._apply(job, delete_duplicates))
        return job

    @classmethod
    async def _apply(cls, job: ImportJob, delete_duplicates: bool):
        try:
            async with cls._busy:
                await cls.apply(job, delete_duplicates=delete_duplicates)
            job.status = "done"
        except Exception as exc:
            print(f"[LibraryImport] Import failed for {job.source}: {exc}")
            job.status, job.error = "failed", "apply_failed"

    @classmethod
    async def apply(cls, job: ImportJob, delete_duplicates: bool = False, refresh: bool = True):
        """Move planned files; with delete_duplicates, also drop lower-quality copies and upgrade library files."""
        from app.services.download_service import download_manager

        moved, deleted = [], []
        actions = ("move", "upgrade") if delete_duplicates else ("move",)
        to_move = [entry for entry in job.entries if entry["action"] in actions]
        losers = [entry for entry in job.entries if entry["action"] == "duplicate"] if delete_duplicates else []
        job.total = len(to_move) + len(losers)

        if delete_duplicates:
            # Drop worse copies first so the better one can take the canonical file name.
            for entry in losers:
                deleted += await asyncio.to_thread(_delete_copy, entry["source"], entry["final"], entry["kept_current"])
                job.processed += 1
            for entry in to_move:
                for path in entry.get("replaces", []):
                    deleted += await asyncio.to_thread(_delete_copy, path, entry["target"], entry["source"])

        for entry in to_move:
            record = await asyncio.to_thread(_move_entry, entry)
            if record:
                if entry["target"].lower().endswith(".mp3"):
                    release = ReleaseInfo(matched=entry["matched"], **entry["release"])
                    await asyncio.to_thread(download_manager._tag_audio, entry["target"], release)
                moved.append(record)
            job.processed += 1

        renamed = []
        if delete_duplicates:
            # A library copy that won keeps its place but gets the canonical name.
            for entry in losers:
                rename = entry.get("rename")
                if rename and os.path.exists(rename[0]) and not os.path.exists(rename[1]):
                    record = await asyncio.to_thread(_move_entry, {"source": rename[0], "target": rename[1]})
                    if record:
                        renamed.append(record)
        job.moved = len(moved)
        job.deleted = sum(path.lower().endswith(AUDIO_EXTENSIONS) for path in deleted)

        if job.upload_session:
            cls.discard_upload(job.upload_session)
        else:
            protected = {root["path"] for root in browse_roots()}
            await asyncio.to_thread(_clean_source, job.source, protected)

        manifest_dir = os.path.dirname(os.environ.get("MUSIC_SEERR_CONFIG") or "/app/data/config.json")
        if os.path.isdir(manifest_dir):
            job.manifest = os.path.join(manifest_dir, f"import-{time.strftime('%Y%m%d-%H%M%S')}.json")
            with open(job.manifest, "w", encoding="utf-8") as handle:
                json.dump({"source": job.label, "moved": moved, "renamed": renamed, "deleted": deleted,
                           "delete_duplicates": delete_duplicates, "plan": job.entries}, handle, indent=2, ensure_ascii=False)

        if refresh and (moved or deleted) and settings.JELLYFIN_API_KEY:
            from app.services.jellyfin_service import JellyfinService
            await JellyfinService.refresh_library()

    @classmethod
    def discard(cls, job_id: str):
        job = cls.jobs.get(job_id)
        if not job or job.status in ("scanning", "applying"):
            raise ValueError("job_not_ready")
        cls.jobs.pop(job_id, None)
        cls.discard_upload(job.upload_session)


def _move_entry(entry: dict) -> Optional[dict]:
    source, target = entry["source"], entry["target"]
    if not os.path.exists(source):
        return None
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if os.path.exists(target):
        print(f"[LibraryImport] Target appeared meanwhile, skipped: {target}")
        return None
    shutil.move(source, target)
    record = {"source": source, "target": target, "side_files": []}
    source_stem, target_stem = os.path.splitext(source)[0], os.path.splitext(target)[0]
    for ext in SIDE_FILE_EXTENSIONS:
        if os.path.exists(source_stem + ext) and not os.path.exists(target_stem + ext):
            shutil.move(source_stem + ext, target_stem + ext)
            record["side_files"].append([source_stem + ext, target_stem + ext])
    return record


def _settle_slot(group: List[dict]):
    """Decide which copy of one track (library file or imported file) survives."""
    sources = {os.path.realpath(entry["source"]) for entry in group}
    library = list(dict.fromkeys(path for entry in group for path in entry.pop("existing")
                                 if os.path.realpath(path) not in sources))
    qualities = {path: audio_quality(path) for path in library + [entry["source"] for entry in group]}
    for entry in group:
        entry["quality"] = qualities[entry["source"]]["label"]
    # Highest quality wins; on a tie the file already in the library stays, then the first imported one.
    candidates = [(path, None) for path in library] + [(entry["source"], entry) for entry in group]
    best_path, best_entry = max(enumerate(candidates), key=lambda item: (qualities[item[1][0]]["score"], -item[0]))[1]

    if best_entry is not None:
        final = best_entry["target"]
        best_entry["replaces"] = library
        if os.path.realpath(best_entry["source"]) == os.path.realpath(final):
            best_entry["action"] = "unchanged"
        else:
            best_entry["action"] = "upgrade" if library else "move"
    else:
        reference = group[0]["target"]
        final = os.path.join(os.path.dirname(reference),
                             os.path.splitext(os.path.basename(reference))[0] + os.path.splitext(best_path)[1].lower())
    for entry in group:
        if entry is best_entry:
            continue
        entry.update(action="duplicate", kept=best_path if best_entry is None else best_entry["target"],
                     kept_current=best_path, kept_quality=qualities[best_path]["label"], final=final)
        if best_entry is None and os.path.realpath(best_path) != os.path.realpath(final):
            entry["rename"] = [best_path, final]


def _delete_copy(path: str, keeper: str, keeper_current: str) -> List[str]:
    """Delete a lower-quality copy; hand its lyrics to the kept file (final name `keeper`) when that has none."""
    if not os.path.exists(path) or not os.path.exists(keeper_current) \
            or os.path.realpath(path) == os.path.realpath(keeper_current):
        return []
    removed = [path]
    os.remove(path)
    stem, keeper_stem = os.path.splitext(path)[0], os.path.splitext(keeper)[0]
    current_stem = os.path.splitext(keeper_current)[0]
    for ext in SIDE_FILE_EXTENSIONS:
        if not os.path.exists(stem + ext) or stem == current_stem:
            # Missing, or shared with the kept file (same name, other format).
            continue
        if stem == keeper_stem and not os.path.exists(current_stem + ext):
            continue
        if stem == keeper_stem:
            # The kept file brings its own lyrics to this name.
            os.remove(stem + ext)
            removed.append(stem + ext)
        elif os.path.exists(keeper_stem + ext) or os.path.exists(current_stem + ext):
            os.remove(stem + ext)
            removed.append(stem + ext)
        else:
            os.makedirs(os.path.dirname(keeper_stem), exist_ok=True)
            shutil.move(stem + ext, keeper_stem + ext)
    return removed


def _orphan_side_file(folder: str, name: str, names: List[str]) -> bool:
    stem, ext = os.path.splitext(name)
    return ext.lower() in SIDE_FILE_EXTENSIONS and not any(
        os.path.splitext(other)[0] == stem and other.lower().endswith(AUDIO_EXTENSIONS) for other in names)


def _clean_source(source_root: str, protected: set):
    """Remove folders in the imported tree that hold no music any more (only metadata, artwork, or orphan lyrics)."""
    for current, dirs, files in os.walk(source_root, topdown=False):
        if current in protected or any(part.startswith(".") for part in os.path.relpath(current, source_root).split(os.sep) if part != "."):
            continue
        names = os.listdir(current)
        if any(os.path.isdir(os.path.join(current, name)) for name in names):
            continue
        if all(name.lower().endswith(DISPOSABLE_EXTENSIONS) or name.startswith("._")
               or _orphan_side_file(current, name, names) for name in names):
            for name in names:
                os.remove(os.path.join(current, name))
            os.rmdir(current)
