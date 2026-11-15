"""Tidy artist credits and album folders already in the library (dry run by default).

Run inside the container:
    python -m app.tools.tidy_library                                 # show the plan
    python -m app.tools.tidy_library --alias "tha Supreme=thasup"    # also map a former artist name
    python -m app.tools.tidy_library --apply                         # write tags, move folders, refresh Jellyfin
    python -m app.tools.tidy_library --undo /app/data/tidy-<timestamp>.json

What it fixes:
- Artist tags such as "Main feat. Guest" or "A, B & C" become one tag value per artist, so Jellyfin lists
  the track under each artist instead of creating a combined artist. Names joined by "&" or "," are only
  split when one of the parts is an artist that has its own folder (keeps "Chase & Status" intact).
- Artist names are spelled like the artist's library folder; configured aliases map former names.
- Album folders of the same album that differ only by year, or that sit under an aliased artist folder,
  are merged into one folder, and every track in an album folder carries the folder's year.
Every applied run writes /app/data/tidy-<timestamp>.json with the previous tags and moves for --undo.
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import time
from collections import defaultdict
from typing import Dict, List, Optional

import mutagen

from app.config import load_webapp_config, save_artist_aliases, settings
from app.services.library_import import DISPOSABLE_EXTENSIONS, SIDE_FILE_EXTENSIONS
from app.services.library_layout import AUDIO_EXTENSIONS, YEAR_SUFFIX, normalize_key

GUEST_MARKER = re.compile(r"\s+[\(\[]?\s*(?:feat\.?|ft\.?|featuring|with|vs\.?)\s+", re.IGNORECASE)
JOINER = re.compile(r"\s*(?:,|&|\s+and\s+|\s+x\s+)\s*", re.IGNORECASE)
DISCOGS_SUFFIX = re.compile(r"\s*\(\d+\)$")
FOLDER_YEAR = re.compile(r"[\(\[](\d{4})[\)\]]\s*$")


class Library:
    def __init__(self, root: str, aliases: Dict[str, str]):
        self.root = root
        self.folders = sorted(e.name for e in os.scandir(root) if e.is_dir() and not e.name.startswith("."))
        self.aliases = {normalize_key(old): new for old, new in aliases.items()}
        # Spelling used by the library for each artist that has its own folder.
        self.known = {normalize_key(name): name for name in self.folders if normalize_key(name)}

    def canonical(self, name: str) -> str:
        name = name.strip()
        if name.count("(") < name.count(")") or name.count("[") < name.count("]"):
            name = name.rstrip(")]").strip()  # "(feat. Guest)" leaves a dangling bracket behind
        name = DISCOGS_SUFFIX.sub("", name)
        key = normalize_key(name)
        name = self.aliases.get(key, name)
        name = self.known.get(normalize_key(name), name)
        # Typographic hyphens (U+2010/U+2011) make Jellyfin list "Anne‐Marie" apart from "Anne-Marie".
        return name.replace("\u2010", "-").replace("\u2011", "-")

    def _split_joined(self, part: str, force: bool) -> List[str]:
        """Split "A & B" / "A, B" when forced (guest list) or when one piece is a known artist."""
        if normalize_key(part) in self.known or normalize_key(part) in self.aliases:
            return [part]
        pieces = [p for p in JOINER.split(part) if p.strip()]
        if len(pieces) > 1 and (force or any(normalize_key(p) in self.known for p in pieces)):
            # " and " / " x " only separate names when a piece is a known artist.
            if not any(normalize_key(p) in self.known for p in pieces):
                # A guest list "A, B and C" still uses "and"; elsewhere it may be part of a name.
                joiner = r"\s*(?:,|&|\s+and\s+)\s*" if force and "," in part else r"\s*[,&]\s*"
                pieces = [p for p in re.split(joiner, part, flags=re.IGNORECASE) if p.strip()]
            return pieces
        return [part]

    def split_credit(self, credit: str) -> List[str]:
        segments = GUEST_MARKER.split(credit.strip())
        names = self._split_joined(segments[0], force=False)
        for guest in segments[1:]:
            names += self._split_joined(guest, force=True)
        result, seen = [], set()
        for name in names:
            name = self.canonical(name)
            if "://" in name:
                continue  # Rip-site watermarks such as "https://example" are not artists.
            if name and normalize_key(name) not in seen:
                seen.add(normalize_key(name))
                result.append(name)
        return result


def _read(path: str):
    try:
        return mutagen.File(path, easy=True)
    except Exception as exc:
        print(f"skip (unreadable) {path}: {exc}")
        return None


def _values(audio, key: str) -> List[str]:
    try:
        return [str(v) for v in (audio.tags or {}).get(key, [])] if audio is not None else []
    except Exception:
        return []


def plan_folders(lib: Library) -> List[dict]:
    """Album folders to merge: aliased artist folders and same album split by year."""
    moves = []
    for folder in lib.folders:
        target_artist = lib.canonical(folder)
        if normalize_key(target_artist) == normalize_key(folder) and target_artist == folder:
            target_artist = folder
        albums = defaultdict(list)
        for artist in {folder, target_artist}:
            artist_path = os.path.join(lib.root, artist)
            if not os.path.isdir(artist_path):
                continue
            for entry in os.scandir(artist_path):
                key = normalize_key(YEAR_SUFFIX.sub("", entry.name))
                if entry.is_dir() and key:
                    albums[key].append((artist, entry.name, sum(f.lower().endswith(AUDIO_EXTENSIONS) for f in os.listdir(entry.path))))
        for versions in albums.values():
            if len(versions) < 2 and versions[0][0] == target_artist:
                continue
            # Keep the folder with the most tracks (the later year on a tie) under the canonical artist.
            keep = sorted(versions, key=lambda v: (v[2], v[1]), reverse=True)[0]
            target_dir = os.path.join(target_artist, keep[1])
            for artist, album, _ in versions:
                source_dir = os.path.join(artist, album)
                if source_dir != target_dir:
                    moves.append({"source_dir": source_dir, "target_dir": target_dir,
                                  "old_artist": artist, "new_artist": target_artist})
    unique = {}
    for move in moves:
        unique.setdefault(move["source_dir"], move)
    return list(unique.values())


def _target_name(name: str, move: dict) -> str:
    prefix = move["old_artist"] + " - "
    return move["new_artist"] + " - " + name[len(prefix):] if name.startswith(prefix) else name


def plan_tags(lib: Library, moves: List[dict]) -> List[dict]:
    moved_dirs = {m["source_dir"]: m["target_dir"] for m in moves}
    tracks = []
    for current, dirs, files in os.walk(lib.root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        rel_dir = os.path.relpath(current, lib.root)
        for name in sorted(files):
            if name.startswith("._") or not name.lower().endswith(AUDIO_EXTENSIONS):
                continue  # "._" files are macOS metadata, not audio
            audio = _read(os.path.join(current, name))
            if audio is not None and audio.tags is not None:
                old = {key: _values(audio, key) for key in ("artist", "albumartist", "date")}
                tracks.append((rel_dir, moved_dirs.get(rel_dir, rel_dir), name, old))

    # Only albums whose tracks disagree on the year (or that were merged) take the folder's year.
    years_by_dir = defaultdict(set)
    for _, final_dir, _, old in tracks:
        years_by_dir[final_dir].add((old["date"] or [""])[0][:4])

    changes = []
    for rel_dir, final_dir, name, old in tracks:
        new = dict(old)
        artists = []
        for credit in old["artist"]:
            artists += [a for a in lib.split_credit(credit) if normalize_key(a) not in {normalize_key(x) for x in artists}]
        if artists:
            new["artist"] = artists
        if old["albumartist"]:
            new["albumartist"] = [lib.canonical(old["albumartist"][0])]
        year_match = FOLDER_YEAR.search(final_dir) if final_dir.count(os.sep) == 1 else None
        mixed = len(years_by_dir[final_dir]) > 1 or rel_dir != final_dir
        if year_match and mixed and old["date"] and not old["date"][0].startswith(year_match.group(1)):
            new["date"] = [year_match.group(1)]
        if new != old:
            changes.append({"path": os.path.join(rel_dir, name), "old": old, "new": new})
    return changes


def apply(lib: Library, moves: List[dict], changes: List[dict]) -> dict:
    manifest = {"root": lib.root, "tags": [], "moves": []}
    for change in changes:
        path = os.path.join(lib.root, change["path"])
        audio = _read(path)
        if audio is None:
            continue
        try:
            for key, value in change["new"].items():
                if value != change["old"][key]:
                    audio[key] = value
            audio.save()
            manifest["tags"].append(change)
        except Exception as exc:
            print(f"tag write failed {change['path']}: {exc}")
    for move in moves:
        source_dir = os.path.join(lib.root, move["source_dir"])
        target_dir = os.path.join(lib.root, move["target_dir"])
        os.makedirs(target_dir, exist_ok=True)
        for name in sorted(os.listdir(source_dir)):
            source = os.path.join(source_dir, name)
            target = os.path.join(target_dir, _target_name(name, move))
            if name.lower().endswith(AUDIO_EXTENSIONS + SIDE_FILE_EXTENSIONS) and not os.path.exists(target):
                shutil.move(source, target)
                manifest["moves"].append([os.path.relpath(source, lib.root), os.path.relpath(target, lib.root)])
        leftovers = os.listdir(source_dir)
        if all(n.lower().endswith(DISPOSABLE_EXTENSIONS) or n.startswith("._") for n in leftovers):
            for n in leftovers:
                os.remove(os.path.join(source_dir, n))
            os.rmdir(source_dir)
            artist_dir = os.path.dirname(source_dir)
            rest = os.listdir(artist_dir)
            if all(n.lower().endswith(DISPOSABLE_EXTENSIONS) for n in rest):
                for n in rest:
                    os.remove(os.path.join(artist_dir, n))
                os.rmdir(artist_dir)
        else:
            print(f"kept {move['source_dir']}: still holds {leftovers}")
    return manifest


def undo(manifest_path: str):
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    root = manifest["root"]
    for source, target in reversed(manifest["moves"]):
        os.makedirs(os.path.dirname(os.path.join(root, source)), exist_ok=True)
        if os.path.exists(os.path.join(root, target)) and not os.path.exists(os.path.join(root, source)):
            shutil.move(os.path.join(root, target), os.path.join(root, source))
    for change in manifest["tags"]:
        audio = _read(os.path.join(root, change["path"]))
        if audio is None:
            continue
        for key, value in change["old"].items():
            if value:
                audio[key] = value
            elif key in audio:
                del audio[key]
        audio.save()
    print(f"Restored {len(manifest['tags'])} tag sets and {len(manifest['moves'])} moved files.")


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--alias", action="append", default=[], metavar="OLD=NEW",
                        help="Map a former or alternate artist name to the library name (saved in the config)")
    parser.add_argument("--apply", action="store_true", help="Write the changes (default is a dry run)")
    parser.add_argument("--undo", metavar="MANIFEST", help="Restore tags and moves recorded by an earlier run")
    parser.add_argument("--manifest", metavar="PATH", help="Where to write the undo manifest (default /app/data/tidy-<timestamp>.json)")
    parser.add_argument("--verbose", action="store_true", help="List every tag change")
    args = parser.parse_args()
    load_webapp_config()

    if args.undo:
        undo(args.undo)
        return

    aliases = dict(settings.ARTIST_ALIASES or {})
    for item in args.alias:
        old, sep, new = item.partition("=")
        if not sep or not old.strip() or not new.strip():
            sys.exit(f"Invalid alias {item!r}; use OLD=NEW")
        aliases[old.strip()] = new.strip()

    lib = Library(os.path.realpath(settings.MUSIC_OUTPUT_DIR), aliases)
    moves = plan_folders(lib)
    changes = plan_tags(lib, moves)
    for move in moves:
        print(f"merge  {move['source_dir']} -> {move['target_dir']}")
    credits = defaultdict(int)
    for change in changes:
        if change["new"]["artist"] != change["old"]["artist"]:
            credits[(" / ".join(change["old"]["artist"]), " | ".join(change["new"]["artist"]))] += 1
        if args.verbose:
            print(f"tags   {change['path']}: {change['old']} -> {change['new']}")
    for (old, new), count in sorted(credits.items()):
        print(f"artist {old}  ->  {new}  ({count})")
    years = sum(c["new"]["date"] != c["old"]["date"] for c in changes)
    album_artists = sum(c["new"]["albumartist"] != c["old"]["albumartist"] for c in changes)
    print(f"\nSummary: {len(moves)} folder merges, {len(changes)} files retagged "
          f"({len(credits)} artist credits, {album_artists} album artists, {years} years)")
    if not args.apply:
        print("Dry run only. Re-run with --apply to write the changes.")
        return

    manifest = apply(lib, moves, changes)
    path = args.manifest or os.path.join(
        os.path.dirname(os.path.realpath(os.environ.get("MUSIC_SEERR_CONFIG", "/app/data/config.json"))),
        f"tidy-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=1)
    if aliases != (settings.ARTIST_ALIASES or {}):
        try:
            save_artist_aliases(aliases)
        except (OSError, ValueError) as exc:
            print(f"Could not save the aliases to the config ({exc}); re-run with --alias as the service user.")
    from app.services.jellyfin_service import JellyfinService
    if settings.JELLYFIN_API_KEY:
        await JellyfinService.refresh_library()
    print(f"Retagged {len(manifest['tags'])} files, moved {len(manifest['moves'])} files. Manifest: {path}")


if __name__ == "__main__":
    asyncio.run(main())
