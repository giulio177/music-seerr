"""Two-way listening sync with Jellyfin.

- Music Seerr -> Jellyfin: a library track listened past the scrobble threshold is marked played for that
  Jellyfin user (play count and last played date), once per play.
- Jellyfin -> Music Seerr: other Jellyfin clients' plays of users with a Music Seerr session are imported,
  live from `/Sessions` (position = listened time) and from each user's recently played items as fallback.
"""
import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.config import settings
from app.services.jellyfin_service import JellyfinService
from app.services.user_data_service import UserDataService

SYNC_INTERVAL = 20
ON_DEMAND_MIN_GAP = 8
BACKFILL_DAYS = 30
OWN_CLIENT = "MusicSeerr"
JELLYFIN_ID = re.compile(r"^[0-9a-f]{32}$")
WATERMARK_PREF = "jellyfin_play_watermark"


def scrobble_threshold(duration: float) -> float:
    """Seconds a play needs to count as a listen: half the track, at most four minutes."""
    return min(240.0, max(duration, 30.0) * 0.5)


def _parse_date(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    match = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?", value)
    if not match:
        return None
    base = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    return base + (float(match.group(2)) if match.group(2) else 0.0)


def _track(item: dict) -> dict:
    data = JellyfinService._format_track_item(item)
    return {"id": data["id"], "title": data["title"], "artist": data["artist"], "album": data["album"], "duration": float(data["duration_sec"] or 0)}


class PlaySync:
    # Jellyfin session ID -> the play row being followed for that session.
    _active: dict = {}
    _lock = asyncio.Lock()
    _last_run = 0.0
    _task: Optional[asyncio.Task] = None

    @staticmethod
    def _base() -> Optional[str]:
        if not settings.JELLYFIN_URL or not settings.JELLYFIN_API_KEY:
            return None
        return settings.JELLYFIN_URL.rstrip("/")

    @classmethod
    async def mark_played(cls, user_id: str, item_id: str) -> bool:
        base = cls._base()
        if not base or not JELLYFIN_ID.match(item_id or "") or user_id.startswith("guest:"):
            return False
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(f"{base}/UserPlayedItems/{item_id}", params={"userId": user_id}, headers=JellyfinService._headers())
                if res.status_code == 404:
                    res = await client.post(f"{base}/Users/{user_id}/PlayedItems/{item_id}", headers=JellyfinService._headers())
                return res.status_code in (200, 204)
        except httpx.HTTPError as exc:
            print(f"[PlaySync] Could not mark {item_id} played: {exc}")
            return False

    @classmethod
    async def report_progress(cls, user: dict, play_id: int, listened: float, duration: float) -> Optional[dict]:
        """Store listened time; the first time a Jellyfin track crosses the threshold, mark it played in Jellyfin."""
        row = UserDataService.update_play_progress(user, play_id, listened, duration)
        if not row:
            return None
        if (row["source"] == "jellyfin" and not row["jellyfin_marked"] and row["duration"] > 0
                and row["listened_seconds"] >= scrobble_threshold(row["duration"])
                and UserDataService.claim_jellyfin_mark(play_id)):
            asyncio.create_task(cls.mark_played(row["user_id"], row["track_id"]))
        return row

    @classmethod
    async def sync(cls, user_id: Optional[str] = None):
        """Import Jellyfin plays; on demand calls (one user) are throttled and never wait for a running sync."""
        if not cls._base():
            return
        if user_id and (cls._lock.locked() or time.time() - cls._last_run < ON_DEMAND_MIN_GAP):
            return
        async with cls._lock:
            users = {u["id"]: u["name"] for u in UserDataService.jellyfin_session_users()}
            if not users:
                return
            try:
                async with httpx.AsyncClient(timeout=8.0, headers=JellyfinService._headers()) as client:
                    await cls._sync_sessions(client, users)
                    for uid, name in users.items():
                        await cls._sync_played(client, uid, name)
            except httpx.HTTPError as exc:
                print(f"[PlaySync] Jellyfin sync failed: {exc}")
            finally:
                cls._last_run = time.time()

    @classmethod
    async def _sync_sessions(cls, client: httpx.AsyncClient, users: dict):
        res = await client.get(f"{cls._base()}/Sessions", params={"activeWithinSeconds": 120})
        if res.status_code != 200:
            return
        now = time.time()
        seen = set()
        for session in res.json():
            user_id = session.get("UserId")
            item = session.get("NowPlayingItem") or {}
            if user_id not in users or item.get("Type") != "Audio" or session.get("Client") == OWN_CLIENT or not item.get("Id"):
                continue
            key = session.get("Id")
            play_state = session.get("PlayState") or {}
            position = (play_state.get("PositionTicks") or 0) / 10_000_000
            paused = bool(play_state.get("IsPaused"))
            seen.add(key)
            state = cls._active.get(key)
            if state and state["item_id"] == item["Id"]:
                state["paused"] = paused
                UserDataService.set_external_listened(state["play_id"], position)
                continue
            track = _track(item)
            started = now - position
            existing = UserDataService.find_play_near(user_id, track["id"], started - 60, now + 5)
            if existing and existing["origin"] == "jellyfin":
                play_id = existing["id"]
                UserDataService.set_external_listened(play_id, position)
            else:
                play_id = UserDataService.record_external_play(user_id, users[user_id], track, started, position, session.get("Client") or "Jellyfin")
            cls._active[key] = {"item_id": item["Id"], "play_id": play_id, "user_id": user_id, "paused": paused}
        for key in list(cls._active):
            if key not in seen:
                cls._active.pop(key)

    @classmethod
    async def _sync_played(cls, client: httpx.AsyncClient, user_id: str, user_name: str):
        stored = UserDataService.get_preference(user_id, WATERMARK_PREF)
        watermark = float(stored) if stored else time.time() - BACKFILL_DAYS * 86400
        res = await client.get(f"{cls._base()}/Items", params={
            "userId": user_id, "Recursive": "true", "IncludeItemTypes": "Audio", "Filters": "IsPlayed",
            "SortBy": "DatePlayed", "SortOrder": "Descending", "Limit": 200 if not stored else 50,
            "EnableUserData": "true", "Fields": "Artists,Album,AlbumArtist,RunTimeTicks",
        })
        if res.status_code != 200:
            return
        newest = watermark
        for item in res.json().get("Items", []):
            played_at = _parse_date((item.get("UserData") or {}).get("LastPlayedDate"))
            if not played_at or played_at <= watermark:
                break
            newest = max(newest, played_at)
            track = _track(item)
            runtime = track["duration"]
            # A play Music Seerr reported, or one already followed live, is already in the history.
            near = UserDataService.find_play_near(user_id, track["id"], played_at - runtime - 900, played_at + 120)
            if near:
                if near["origin"] == "jellyfin":
                    UserDataService.set_external_listened(near["id"], runtime)
                continue
            UserDataService.record_external_play(user_id, user_name, track, max(watermark, played_at - runtime), runtime, "Jellyfin")
        UserDataService.set_preference(user_id, WATERMARK_PREF, repr(newest))

    @classmethod
    def active_clients(cls, user_id: Optional[str] = None) -> dict:
        """Play row ID -> True for plays currently playing (not paused) in another Jellyfin client."""
        return {state["play_id"]: True for state in cls._active.values()
                if not state["paused"] and (not user_id or state["user_id"] == user_id)}

    @classmethod
    async def _loop(cls):
        while True:
            try:
                await cls.sync()
            except Exception as exc:  # keep the loop alive on unexpected payloads
                print(f"[PlaySync] Unexpected sync error: {exc}")
            await asyncio.sleep(SYNC_INTERVAL)

    @classmethod
    def start(cls):
        if cls._task is None or cls._task.done():
            cls._task = asyncio.create_task(cls._loop())
