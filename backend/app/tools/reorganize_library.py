"""Move previously downloaded tracks into the Lidarr-style library layout.

Run inside the container (dry run by default):
    python -m app.tools.reorganize_library /music/MusicSeerr
    python -m app.tools.reorganize_library /music/MusicSeerr --apply

Every applied move is recorded in /app/data/reorganize-<timestamp>.json so it can be undone.
"""

import argparse
import asyncio
import json
import os
import sys
import time

import mutagen

from app.config import settings
from app.models.schemas import TrackItem
from app.services.library_layout import (
    AUDIO_EXTENSIONS,
    album_directory,
    find_existing_track,
    resolve_release,
    track_stem,
)

SIDE_FILE_EXTENSIONS = (".lrc",)
DISPOSABLE_EXTENSIONS = (".nfo",)


def read_track(path: str, source_root: str) -> TrackItem:
    tags = {}
    duration = 0
    try:
        audio = mutagen.File(path, easy=True)
        if audio is not None:
            tags = {key: (value[0] if isinstance(value, list) and value else value) for key, value in (audio.tags or {}).items()}
            duration = int(getattr(audio.info, "length", 0) or 0)
    except Exception as exc:
        print(f"  ! could not read tags of {path}: {exc}")
    parts = os.path.relpath(path, source_root).split(os.sep)
    stem = os.path.splitext(parts[-1])[0]
    return TrackItem(
        id=f"local:{len(path)}",
        title=str(tags.get("title") or stem)[:300],
        artist=str(tags.get("artist") or (parts[0] if len(parts) > 2 else "Unknown"))[:200],
        album=str(tags.get("album") or (parts[1] if len(parts) > 2 else stem))[:200],
        duration=min(duration, 86400),
    )


async def plan(source_root: str, music_root: str) -> list:
    audio_files = sorted(
        os.path.join(root, name)
        for root, _, files in os.walk(source_root)
        for name in files
        if name.lower().endswith(AUDIO_EXTENSIONS)
    )
    entries = []
    planned_targets = set()
    for index, path in enumerate(audio_files, 1):
        track = read_track(path, source_root)
        release = await resolve_release(track)
        ext = os.path.splitext(path)[1].lower()
        dest_dir = album_directory(music_root, release)
        target = os.path.join(dest_dir, track_stem(release) + ext)
        existing = find_existing_track(dest_dir, release)
        if os.path.realpath(path) == os.path.realpath(target):
            action = "unchanged"
        elif (existing and os.path.realpath(existing) != os.path.realpath(path)) or target in planned_targets:
            action = "duplicate"
        else:
            action = "move"
            planned_targets.add(target)
        entries.append({
            "source": path,
            "target": target,
            "action": action,
            "matched": release.matched,
            "existing": existing,
            "release": {
                "title": release.title, "artist": release.artist, "album_artist": release.album_artist,
                "album": release.album, "year": release.year, "track_number": release.track_number,
                "disc_number": release.disc_number, "isrc": release.isrc, "cover_url": release.cover_url,
            },
        })
        print(f"[{index}/{len(audio_files)}] {action:9} {'' if release.matched else '(no match) '}"
              f"{os.path.relpath(path, music_root)} -> {os.path.relpath(target, music_root)}")
    return entries


def apply(entries: list, music_root: str, source_root: str) -> list:
    from app.services.download_service import download_manager
    from app.services.library_layout import ReleaseInfo

    moved = []
    for entry in entries:
        if entry["action"] != "move":
            continue
        source, target = entry["source"], entry["target"]
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.exists(target):
            print(f"  ! target appeared meanwhile, skipped: {target}")
            continue
        os.rename(source, target)
        record = {"source": source, "target": target, "side_files": []}
        source_stem, target_stem = os.path.splitext(source)[0], os.path.splitext(target)[0]
        for ext in SIDE_FILE_EXTENSIONS:
            if os.path.exists(source_stem + ext) and not os.path.exists(target_stem + ext):
                os.rename(source_stem + ext, target_stem + ext)
                record["side_files"].append([source_stem + ext, target_stem + ext])
        if target.lower().endswith(".mp3"):
            download_manager._tag_audio(target, ReleaseInfo(matched=entry["matched"], **entry["release"]))
        moved.append(record)

    # Remove metadata files that Jellyfin regenerates, then empty folders, inside the source tree only.
    for root, _, files in os.walk(source_root, topdown=False):
        if files and all(name.lower().endswith(DISPOSABLE_EXTENSIONS) for name in files):
            for name in files:
                os.remove(os.path.join(root, name))
        if not os.listdir(root):
            os.rmdir(root)
    return moved


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", help="Folder with tracks to reorganize, inside the music root")
    parser.add_argument("--apply", action="store_true", help="Move files (default is a dry run)")
    args = parser.parse_args()

    music_root = os.path.realpath(settings.MUSIC_OUTPUT_DIR)
    source_root = os.path.realpath(args.source)
    if os.path.commonpath([music_root, source_root]) != music_root:
        sys.exit(f"{source_root} is not inside the music root {music_root}")

    entries = await plan(source_root, music_root)
    counts = {}
    for entry in entries:
        counts[entry["action"]] = counts.get(entry["action"], 0) + 1
    unmatched = sum(not entry["matched"] for entry in entries)
    print(f"\nSummary: {counts}, without online match: {unmatched}")
    if not args.apply:
        print("Dry run only. Re-run with --apply to move the files.")
        return

    moved = apply(entries, music_root, source_root)
    manifest_dir = os.path.dirname(os.environ.get("MUSIC_SEERR_CONFIG") or "/app/data/config.json")
    manifest = os.path.join(manifest_dir, f"reorganize-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(manifest, "w", encoding="utf-8") as handle:
        json.dump({"moved": moved, "plan": entries}, handle, indent=2, ensure_ascii=False)
    print(f"Moved {len(moved)} tracks. Manifest: {manifest}")

    if settings.JELLYFIN_API_KEY:
        from app.services.jellyfin_service import JellyfinService
        await JellyfinService.refresh_library()
        print("Jellyfin library refresh requested.")


if __name__ == "__main__":
    asyncio.run(main())
