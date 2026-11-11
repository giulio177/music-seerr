import os
import csv
import json
import re
import io
import time
import asyncio
from typing import Optional, List, Dict, Any
from fastapi import HTTPException
from app.services.jellyfin_service import JellyfinService
from app.services.search_service import SearchService
from app.services.download_service import download_manager
from app.models.schemas import TrackItem

STORAGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data", "playlist_csvs")

def _normalize_str(val: Optional[str]) -> str:
    if not val:
        return ""
    # Lowercase, remove accents / punctuation for resilient matching
    val = val.lower().strip()
    val = re.sub(r'\(.*?\)|\[.*?\]', '', val) # remove (feat. ...), [remastered], etc.
    val = re.sub(r'[^a-z0-9]', '', val)
    return val

class PlaylistCSVService:
    @staticmethod
    def _ensure_dir():
        os.makedirs(STORAGE_DIR, exist_ok=True)

    @classmethod
    def get_csv_path(cls, playlist_id: str) -> str:
        cls._ensure_dir()
        return os.path.join(STORAGE_DIR, f"{playlist_id}.csv")

    @classmethod
    def get_meta_path(cls, playlist_id: str) -> str:
        cls._ensure_dir()
        return os.path.join(STORAGE_DIR, f"{playlist_id}.meta.json")

    @classmethod
    def save_csv(cls, playlist_id: str, filename: str, content: str) -> Dict[str, Any]:
        cls._ensure_dir()
        csv_path = cls.get_csv_path(playlist_id)
        meta_path = cls.get_meta_path(playlist_id)

        with open(csv_path, "w", encoding="utf-8") as f:
            f.write(content)

        parsed_tracks = cls.parse_csv_text(content)

        meta = {
            "playlist_id": playlist_id,
            "filename": filename,
            "track_count": len(parsed_tracks),
            "row_count": len(parsed_tracks),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "has_csv": True
        }
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

        return meta

    @classmethod
    def get_csv_info(cls, playlist_id: str) -> Dict[str, Any]:
        meta_path = cls.get_meta_path(playlist_id)
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    data["has_csv"] = True
                    data["row_count"] = data.get("track_count", 0)
                    return data
            except Exception:
                pass

        csv_path = cls.get_csv_path(playlist_id)
        if os.path.exists(csv_path):
            try:
                with open(csv_path, "r", encoding="utf-8") as f:
                    tracks = cls.parse_csv_text(f.read())
                return {
                    "playlist_id": playlist_id,
                    "filename": f"{playlist_id}.csv",
                    "track_count": len(tracks),
                    "row_count": len(tracks),
                    "updated_at": "",
                    "has_csv": True
                }
            except Exception:
                pass

        return {
            "playlist_id": playlist_id,
            "filename": None,
            "track_count": 0,
            "row_count": 0,
            "updated_at": None,
            "has_csv": False
        }

    @classmethod
    def parse_csv_text(cls, text: str) -> List[Dict[str, str]]:
        if not text or not text.strip():
            return []

        text = text.lstrip('\ufeff')

        # Detect delimiter
        first_line = text.strip().split("\n")[0]
        delimiter = ','
        if ';' in first_line and first_line.count(';') > first_line.count(','):
            delimiter = ';'
        elif '\t' in first_line and first_line.count('\t') > first_line.count(','):
            delimiter = '\t'

        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows = list(reader)
        if not rows:
            return []

        header = [h.strip() for h in rows[0]]
        title_idx = -1
        artist_idx = -1
        album_idx = -1

        for idx, col in enumerate(header):
            col_clean = col.lower().strip()
            if title_idx == -1 and re.search(r'\b(title|track(\s*name)?|song(\s*name)?|name|titolo|brano)\b', col_clean):
                title_idx = idx
            elif artist_idx == -1 and re.search(r'\b(artist(\s*name)?|artists|artista|artisti|performer|autore)\b', col_clean):
                artist_idx = idx
            elif album_idx == -1 and re.search(r'\b(album(\s*name)?|disco)\b', col_clean):
                album_idx = idx

        # If header wasn't standard or recognized, fallback by position if 2+ columns
        has_header = (title_idx != -1 or artist_idx != -1)
        data_rows = rows[1:] if has_header else rows

        if title_idx == -1:
            title_idx = 0
        if artist_idx == -1:
            artist_idx = 1 if len(header) > 1 else -1
        if album_idx == -1 and len(header) > 2:
            album_idx = 2

        tracks = []
        for r in data_rows:
            if not r or len(r) == 0:
                continue
            title = r[title_idx].strip() if title_idx < len(r) and title_idx >= 0 else ""
            artist = r[artist_idx].strip() if artist_idx < len(r) and artist_idx >= 0 else ""
            album = r[album_idx].strip() if album_idx < len(r) and album_idx >= 0 else ""

            if not title:
                continue

            tracks.append({
                "title": title,
                "artist": artist,
                "album": album
            })

        return tracks

    @classmethod
    async def match_playlist(cls, playlist_id: str, csv_content: Optional[str] = None, user_id: Optional[str] = None) -> Dict[str, Any]:
        if not csv_content:
            csv_path = cls.get_csv_path(playlist_id)
            if not os.path.exists(csv_path):
                return {
                    "error": "Nessun CSV accoppiato a questa playlist.",
                    "summary": {"total": 0, "in_playlist": 0, "in_library": 0, "to_download": 0},
                    "in_playlist": [],
                    "in_library": [],
                    "to_download": [],
                    "not_in_csv": []
                }
            with open(csv_path, "r", encoding="utf-8") as f:
                csv_content = f.read()

        csv_tracks = cls.parse_csv_text(csv_content)
        if not csv_tracks:
            return {
                "summary": {"total": 0, "in_playlist": 0, "in_library": 0, "to_download": 0},
                "in_playlist": [],
                "in_library": [],
                "to_download": [],
                "not_in_csv": []
            }

        # 1. Fetch current tracks in the target playlist
        pl_data = await JellyfinService.get_playlist_tracks(playlist_id, user_id=user_id)
        playlist_tracks = pl_data.get("tracks", [])

        # 2. Fetch full library tracks to cross-reference
        lib_data = await JellyfinService.get_library_tracks(limit=10000, user_id=user_id)
        library_tracks = lib_data.get("tracks", [])

        # Build index lookups
        pl_lookup = {}
        for t in playlist_tracks:
            norm_title = _normalize_str(t.get("title"))
            norm_artist = _normalize_str(t.get("artist"))
            if norm_title:
                pl_lookup[(norm_title, norm_artist)] = t
                if norm_title not in pl_lookup:
                    pl_lookup[norm_title] = t

        lib_lookup = {}
        for t in library_tracks:
            norm_title = _normalize_str(t.get("title"))
            norm_artist = _normalize_str(t.get("artist"))
            if norm_title:
                lib_lookup[(norm_title, norm_artist)] = t
                if norm_title not in lib_lookup:
                    lib_lookup[norm_title] = t

        in_playlist = []
        in_library = []
        to_download = []

        for idx, item in enumerate(csv_tracks):
            title = item.get("title", "")
            artist = item.get("artist", "")
            album = item.get("album", "")
            norm_title = _normalize_str(title)
            norm_artist = _normalize_str(artist)

            record = {
                "csv_index": idx,
                "title": title,
                "artist": artist,
                "album": album
            }

            # Check if in playlist
            matched_pl = pl_lookup.get((norm_title, norm_artist)) or pl_lookup.get(norm_title)
            if matched_pl:
                record["status"] = "in_playlist"
                record["id"] = matched_pl.get("id")
                record["cover_url"] = matched_pl.get("cover_url")
                record["matched_title"] = matched_pl.get("title")
                record["matched_artist"] = matched_pl.get("artist")
                record["matched_track"] = matched_pl
                in_playlist.append(record)
                continue

            # Check if in library
            matched_lib = lib_lookup.get((norm_title, norm_artist)) or lib_lookup.get(norm_title)
            if matched_lib:
                record["status"] = "in_library"
                record["id"] = matched_lib.get("id")
                record["cover_url"] = matched_lib.get("cover_url")
                record["matched_title"] = matched_lib.get("title")
                record["matched_artist"] = matched_lib.get("artist")
                record["matched_track"] = matched_lib
                in_library.append(record)
                continue

            # Otherwise, to download
            record["status"] = "to_download"
            record["matched_track"] = None
            to_download.append(record)

        # Attach artwork to missing CSV tracks so the matcher remains visual.
        artwork_slots = asyncio.Semaphore(6)

        async def add_artwork(record):
            async with artwork_slots:
                candidates = await SearchService.search_deezer(
                    f"{record.get('artist', '')} {record.get('title', '')}".strip(), limit=5
                )
            if not candidates:
                return
            target_title = _normalize_str(record.get("title"))
            target_artist = _normalize_str(record.get("artist"))
            best = max(
                candidates,
                key=lambda track: (
                    2 if _normalize_str(track.title) == target_title else 0,
                    1 if target_artist and target_artist in _normalize_str(track.artist) else 0,
                ),
            )
            record["cover_url"] = best.cover_url
            record["matched_title"] = best.title

        await asyncio.gather(*(add_artwork(record) for record in to_download), return_exceptions=True)

        # Include playlist entries absent from the CSV as a separate, read-only group.
        csv_keys = {
            (_normalize_str(item.get("title")), _normalize_str(item.get("artist")))
            for item in csv_tracks
        }
        not_in_csv = [
            {**track, "status": "not_in_csv", "csv_index": index}
            for index, track in enumerate(playlist_tracks, start=1)
            if (_normalize_str(track.get("title")), _normalize_str(track.get("artist"))) not in csv_keys
        ]

        all_results = sorted(in_playlist + in_library + to_download, key=lambda x: x["csv_index"])
        counts = {
            "in_playlist": len(in_playlist),
            "in_library": len(in_library),
            "to_download": len(to_download)
        }

        return {
            "total_csv_tracks": len(csv_tracks),
            "counts": counts,
            "summary": {
                "total": len(csv_tracks),
                **counts
            },
            "results": all_results,
            "not_in_csv": not_in_csv,
            "not_in_csv_count": len(not_in_csv),
            "in_playlist": in_playlist,
            "in_library": in_library,
            "to_download": to_download
        }

    @classmethod
    async def add_tracks_to_playlist(cls, playlist_id: str, item_ids: List[str], user_id: Optional[str] = None) -> Dict[str, Any]:
        added = 0
        failed = 0
        for item_id in item_ids:
            ok = await JellyfinService.add_to_playlist(playlist_id, item_id, user_id=user_id)
            if ok:
                added += 1
            else:
                failed += 1
        return {"added": added, "failed": failed}

    @classmethod
    async def queue_tracks_download(cls, playlist_id: str, playlist_name: str, tracks: List[Dict[str, str]], owner: dict, playlist_user_id: Optional[str] = None) -> Dict[str, Any]:
        requested = [item for item in tracks if item.get("title", "").strip()]
        if len(requested) > download_manager.MAX_ACTIVE_JOBS:
            raise HTTPException(status_code=413, detail="Puoi inviare al massimo 500 brani in una singola operazione.")
        active = sum(job.status in download_manager.ACTIVE_STATUSES for job in download_manager.jobs.values())
        if active + len(requested) > download_manager.MAX_ACTIVE_JOBS:
            raise HTTPException(status_code=429, detail="La coda può contenere al massimo 500 download attivi. Attendi che si liberino alcuni posti.")

        def build_track(item):
            title = item.get("title", "").strip()
            artist = item.get("artist", "").strip()
            album = item.get("album", "").strip()
            safe_id = re.sub(r'[^a-zA-Z0-9]', '_', f"{artist}_{title}")[:32]
            return TrackItem(
                id=safe_id or f"csv_{len(title)}",
                title=title,
                artist=artist or "Artista",
                album=album or "Album",
                duration=max(0, int(item.get("duration") or 0)),
                cover_url=item.get("cover_url") or None,
                source="csv"
            )

        matched_tracks = [build_track(item) for item in requested]
        for item, matched_track in zip(requested, matched_tracks):
            await download_manager.create_job(
                track=matched_track,
                playlist_id=playlist_id,
                playlist_name=playlist_name,
                owner=owner,
                playlist_user_id=playlist_user_id or owner.get("id")
            )
        queued = len(matched_tracks)

        return {"queued": queued}
