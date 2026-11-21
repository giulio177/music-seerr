"""Persistent per-user application history and server-side sessions."""

import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from typing import Any, List, Optional


# Columns added after the first release; created on startup when missing.
PLAY_HISTORY_COLUMNS = {
    "listened_seconds": "REAL NOT NULL DEFAULT 0",
    "duration": "REAL NOT NULL DEFAULT 0",
    "cover_url": "TEXT NOT NULL DEFAULT ''",
    "origin": "TEXT NOT NULL DEFAULT 'app'",
    "client": "TEXT NOT NULL DEFAULT ''",
    "jellyfin_marked": "INTEGER NOT NULL DEFAULT 0",
    "updated_at": "REAL NOT NULL DEFAULT 0",
}


class UserDataService:
    SESSION_TTL = 60 * 60 * 24 * 30
    _db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "music_seerr.sqlite3")

    @classmethod
    def _connect(cls):
        data_dir = os.path.dirname(cls._db_path)
        # Bind-mounted data directories must be permissioned by the host operator;
        # a non-root container user cannot chmod an existing mountpoint.
        os.makedirs(data_dir, mode=0o700, exist_ok=True)
        db = sqlite3.connect(cls._db_path, timeout=10)
        try:
            os.chmod(cls._db_path, 0o600)
        except PermissionError:
            # The containing private directory still protects the database file.
            pass
        db.row_factory = sqlite3.Row
        return db

    @classmethod
    def initialize(cls):
        with cls._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS app_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    is_admin INTEGER NOT NULL DEFAULT 0,
                    is_guest INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    jellyfin_credentials TEXT
                );
                CREATE TABLE IF NOT EXISTS download_history (
                    job_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    user_name TEXT NOT NULL,
                    track_json TEXT NOT NULL,
                    playlist_id TEXT,
                    playlist_name TEXT,
                    status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS download_history_user_time ON download_history(user_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS play_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    user_name TEXT NOT NULL,
                    track_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    artist TEXT NOT NULL,
                    album TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'preview',
                    played_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS play_history_user_time ON play_history(user_id, played_at DESC);
                CREATE TABLE IF NOT EXISTS user_preferences (
                    user_id TEXT NOT NULL,
                    preference TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY(user_id, preference)
                );
                CREATE TABLE IF NOT EXISTS health_ignores (
                    issue_id TEXT PRIMARY KEY,
                    path TEXT NOT NULL,
                    code TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS duplicate_ignores (
                    item_a TEXT NOT NULL,
                    item_b TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(item_a, item_b)
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(play_history)")}
            for name, ddl in PLAY_HISTORY_COLUMNS.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE play_history ADD COLUMN {name} {ddl}")
            db.execute("CREATE INDEX IF NOT EXISTS play_history_user_track ON play_history(user_id, track_id, played_at)")
            session_columns = {row["name"] for row in db.execute("PRAGMA table_info(app_sessions)")}
            if "jellyfin_credentials" not in session_columns:
                db.execute("ALTER TABLE app_sessions ADD COLUMN jellyfin_credentials TEXT")
            db.execute("UPDATE download_history SET status='interrupted', updated_at=? WHERE status IN ('pending','downloading','tagging','jellyfin_sync')", (time.time(),))

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    # The signed-in user's own Jellyfin token (plus the device ID it was issued for) is kept encrypted with the
    # session, only for calls Jellyfin refuses with the server API key (moving playlist entries). It never
    # reaches the browser and is revoked in Jellyfin when the session ends.
    @classmethod
    def _seal_credentials(cls, credentials: Optional[dict]) -> Optional[str]:
        if not credentials or not credentials.get("token") or not credentials.get("device_id"):
            return None
        from app.services import secret_box
        return secret_box.encrypt(json.dumps({"token": credentials["token"], "device_id": credentials["device_id"]}),
                                  os.path.dirname(cls._db_path))

    @classmethod
    def _open_credentials(cls, sealed: Optional[str]) -> Optional[dict]:
        from app.services import secret_box
        raw = secret_box.decrypt(sealed, os.path.dirname(cls._db_path))
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            return None
        return data if isinstance(data, dict) and data.get("token") and data.get("device_id") else None

    @classmethod
    def create_session(cls, user: Optional[dict] = None, is_guest: bool = False, jellyfin_credentials: Optional[dict] = None) -> str:
        now = time.time()
        token = secrets.token_urlsafe(36)
        user = user or {}
        user_id = str(user.get("id") or f"guest:{uuid.uuid4()}")
        name = str(user.get("name") or "Guest")
        is_admin = bool(user.get("is_admin", False)) and not is_guest
        sealed = None if is_guest else cls._seal_credentials(jellyfin_credentials)
        with cls._connect() as db:
            db.execute(
                "INSERT INTO app_sessions(token_hash,user_id,display_name,is_admin,is_guest,created_at,expires_at,jellyfin_credentials) VALUES(?,?,?,?,?,?,?,?)",
                (cls._hash_token(token), user_id, name, int(is_admin), int(is_guest), now, now + cls.SESSION_TTL, sealed),
            )
        return token

    @classmethod
    def jellyfin_credentials(cls, token: Optional[str]) -> Optional[dict]:
        """The session's own Jellyfin token and device ID, or None (guest, older session, unreadable key)."""
        if not token:
            return None
        with cls._connect() as db:
            row = db.execute("SELECT jellyfin_credentials FROM app_sessions WHERE token_hash=? AND expires_at>?",
                             (cls._hash_token(token), time.time())).fetchone()
        return cls._open_credentials(row["jellyfin_credentials"]) if row else None

    @classmethod
    def forget_jellyfin_credentials(cls, token: Optional[str]):
        """Drop a stored Jellyfin token that Jellyfin no longer accepts."""
        if token:
            with cls._connect() as db:
                db.execute("UPDATE app_sessions SET jellyfin_credentials=NULL WHERE token_hash=?", (cls._hash_token(token),))

    @classmethod
    def purge_expired_sessions(cls) -> List[dict]:
        """Delete expired sessions; returns their Jellyfin credentials so the caller can revoke them."""
        now = time.time()
        with cls._connect() as db:
            rows = db.execute("SELECT jellyfin_credentials FROM app_sessions WHERE expires_at<=?", (now,)).fetchall()
            db.execute("DELETE FROM app_sessions WHERE expires_at<=?", (now,))
        return [c for c in (cls._open_credentials(row["jellyfin_credentials"]) for row in rows) if c]

    @classmethod
    def get_session(cls, token: Optional[str]) -> Optional[dict]:
        if not token:
            return None
        now = time.time()
        token_hash = cls._hash_token(token)
        with cls._connect() as db:
            row = db.execute("SELECT * FROM app_sessions WHERE token_hash=?", (token_hash,)).fetchone()
            if not row:
                return None
            if row["expires_at"] <= now:
                # Expired rows are removed by purge_expired_sessions, which also revokes their Jellyfin token.
                return None
            return {
                "id": row["user_id"],
                "name": row["display_name"],
                "is_admin": bool(row["is_admin"]),
                "is_guest": bool(row["is_guest"]),
            }

    @classmethod
    def delete_session(cls, token: Optional[str]) -> Optional[dict]:
        """Delete the session; returns its Jellyfin credentials (if any) so the caller can revoke them."""
        if not token:
            return None
        with cls._connect() as db:
            row = db.execute("SELECT jellyfin_credentials FROM app_sessions WHERE token_hash=?", (cls._hash_token(token),)).fetchone()
            db.execute("DELETE FROM app_sessions WHERE token_hash=?", (cls._hash_token(token),))
        return cls._open_credentials(row["jellyfin_credentials"]) if row else None

    @classmethod
    def _user_id(cls, user: dict) -> str:
        return str(user.get("id") or "guest:unknown")

    @classmethod
    def record_download(cls, job_id: str, user: dict, track: dict, playlist_id: Optional[str], playlist_name: Optional[str]):
        now = time.time()
        with cls._connect() as db:
            db.execute("""INSERT INTO download_history
                (job_id,user_id,user_name,track_json,playlist_id,playlist_name,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (job_id, cls._user_id(user), user.get("name", "Guest"), json.dumps(track, ensure_ascii=False), playlist_id, playlist_name, "pending", now, now))

    @classmethod
    def update_download_status(cls, job_id: str, status: str):
        with cls._connect() as db:
            db.execute("UPDATE download_history SET status=?,updated_at=? WHERE job_id=?", (status, time.time(), job_id))

    @classmethod
    def update_download_cover(cls, job_id: str, cover_url: str):
        """Store artwork found during the download so the history row shows it."""
        with cls._connect() as db:
            row = db.execute("SELECT track_json FROM download_history WHERE job_id=?", (job_id,)).fetchone()
            if not row:
                return
            track = json.loads(row["track_json"])
            if track.get("cover_url"):
                return
            track["cover_url"] = cover_url
            db.execute("UPDATE download_history SET track_json=? WHERE job_id=?", (json.dumps(track, ensure_ascii=False), job_id))

    @staticmethod
    def _item_pairs(item_ids: list[str]) -> list[tuple[str, str]]:
        ids = sorted({str(item) for item in item_ids if item})
        return [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]]

    @classmethod
    def list_duplicate_ignores(cls) -> list[list[str]]:
        """Library tracks a user marked as different songs despite matching titles; shared by every account."""
        with cls._connect() as db:
            return [[row["item_a"], row["item_b"]] for row in db.execute("SELECT item_a,item_b FROM duplicate_ignores")]

    @classmethod
    def add_duplicate_ignores(cls, user: dict, item_ids: list[str]):
        now = time.time()
        with cls._connect() as db:
            db.executemany("INSERT OR IGNORE INTO duplicate_ignores(item_a,item_b,user_id,created_at) VALUES(?,?,?,?)",
                           [(a, b, cls._user_id(user), now) for a, b in cls._item_pairs(item_ids)])

    @classmethod
    def remove_duplicate_ignores(cls, item_ids: list[str]):
        with cls._connect() as db:
            db.executemany("DELETE FROM duplicate_ignores WHERE item_a=? AND item_b=?", cls._item_pairs(item_ids))

    @classmethod
    def list_health_ignores(cls) -> set:
        """Library health issues an administrator marked as fine (keyed by issue code and file path)."""
        with cls._connect() as db:
            return {row["issue_id"] for row in db.execute("SELECT issue_id FROM health_ignores")}

    @classmethod
    def add_health_ignore(cls, user: dict, issue: dict):
        with cls._connect() as db:
            db.execute("INSERT OR IGNORE INTO health_ignores(issue_id,path,code,user_id,created_at) VALUES(?,?,?,?,?)",
                       (issue["id"], issue["path"], issue["code"], cls._user_id(user), time.time()))

    @classmethod
    def clear_health_ignores(cls) -> int:
        with cls._connect() as db:
            return db.execute("DELETE FROM health_ignores").rowcount

    @classmethod
    def list_downloads(cls, user_id: Optional[str] = None, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with cls._connect() as db:
            if user_id:
                rows = db.execute("SELECT * FROM download_history WHERE user_id=? ORDER BY created_at DESC LIMIT ?", (user_id, limit)).fetchall()
            else:
                rows = db.execute("SELECT * FROM download_history ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) | {"track": json.loads(row["track_json"])} for row in rows]

    @classmethod
    def record_play(cls, user: dict, track: dict, source: str = "preview") -> Optional[int]:
        """Store a playback start and return its row ID (an existing row when the same track started within 30 s)."""
        user_id = cls._user_id(user)
        track_id = str(track.get("id") or "")[:200]
        title = str(track.get("title") or "")[:300]
        artist = str(track.get("artist") or "")[:300]
        if not track_id or not title:
            return None
        cover = str(track.get("cover_url") or "")
        if not cover.startswith(("https://", "/api/")):
            cover = ""
        try:
            duration = max(0.0, min(float(track.get("duration") or 0), 86400.0))
        except (TypeError, ValueError):
            duration = 0.0
        now = time.time()
        with cls._connect() as db:
            existing = db.execute("""SELECT id FROM play_history
                WHERE user_id=? AND track_id=? AND origin='app' AND played_at>? ORDER BY played_at DESC LIMIT 1""", (user_id, track_id, now - 30)).fetchone()
            if existing:
                return existing["id"]
            cursor = db.execute("""INSERT INTO play_history(user_id,user_name,track_id,title,artist,album,source,played_at,duration,cover_url,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (user_id, user.get("name", "Guest"), track_id, title, artist, str(track.get("album") or "")[:300], source, now, duration, cover[:1000], now))
            return cursor.lastrowid

    @classmethod
    def update_play_progress(cls, user: dict, play_id: int, listened: float, duration: float) -> Optional[dict]:
        """Raise the listened time of the user's own play; returns the updated row."""
        with cls._connect() as db:
            row = db.execute("SELECT * FROM play_history WHERE id=? AND user_id=? AND origin='app'", (play_id, cls._user_id(user))).fetchone()
            if not row:
                return None
            duration = duration if duration > 0 else row["duration"]
            # Listened time can never exceed the wall-clock time since the play started.
            ceiling = time.time() - row["played_at"] + 5
            listened = max(row["listened_seconds"], min(listened, ceiling, duration or ceiling))
            db.execute("UPDATE play_history SET listened_seconds=?, duration=?, updated_at=? WHERE id=?", (listened, duration, time.time(), play_id))
            return dict(row) | {"listened_seconds": listened, "duration": duration}

    @classmethod
    def claim_jellyfin_mark(cls, play_id: int) -> bool:
        """True once per play: the caller then reports it to Jellyfin."""
        with cls._connect() as db:
            return db.execute("UPDATE play_history SET jellyfin_marked=1 WHERE id=? AND jellyfin_marked=0", (play_id,)).rowcount == 1

    @classmethod
    def find_play_near(cls, user_id: str, track_id: str, start: float, end: float) -> Optional[dict]:
        with cls._connect() as db:
            row = db.execute("""SELECT * FROM play_history WHERE user_id=? AND track_id=? AND played_at BETWEEN ? AND ?
                ORDER BY played_at DESC LIMIT 1""", (user_id, track_id, start, end)).fetchone()
        return dict(row) if row else None

    @classmethod
    def record_external_play(cls, user_id: str, user_name: str, track: dict, played_at: float, listened: float, client: str) -> int:
        """A play made in another Jellyfin client."""
        with cls._connect() as db:
            cursor = db.execute("""INSERT INTO play_history(user_id,user_name,track_id,title,artist,album,source,played_at,
                listened_seconds,duration,origin,client,jellyfin_marked,updated_at) VALUES(?,?,?,?,?,?,'jellyfin',?,?,?,'jellyfin',?,1,?)""",
                (user_id, user_name, track["id"], track["title"][:300], track.get("artist", "")[:300], track.get("album", "")[:300],
                 played_at, listened, track.get("duration", 0), client[:100], time.time()))
            return cursor.lastrowid

    @classmethod
    def set_external_listened(cls, play_id: int, listened: float):
        with cls._connect() as db:
            db.execute("""UPDATE play_history SET listened_seconds=MAX(listened_seconds, MIN(?, CASE WHEN duration>0 THEN duration ELSE ? END)),
                updated_at=? WHERE id=? AND origin='jellyfin'""", (listened, listened, time.time(), play_id))

    @classmethod
    def jellyfin_session_users(cls) -> list[dict]:
        """Jellyfin accounts with a live Music Seerr session; their Jellyfin plays are imported."""
        with cls._connect() as db:
            rows = db.execute("""SELECT user_id, MAX(display_name) AS name FROM app_sessions
                WHERE is_guest=0 AND expires_at>? GROUP BY user_id""", (time.time(),)).fetchall()
        return [{"id": row["user_id"], "name": row["name"]} for row in rows]

    @classmethod
    def get_preference(cls, user_id: str, preference: str) -> Optional[str]:
        with cls._connect() as db:
            row = db.execute("SELECT value FROM user_preferences WHERE user_id=? AND preference=?", (user_id, preference)).fetchone()
        return row["value"] if row else None

    @classmethod
    def set_preference(cls, user_id: str, preference: str, value: str):
        with cls._connect() as db:
            db.execute("""INSERT INTO user_preferences(user_id,preference,value) VALUES(?,?,?)
                ON CONFLICT(user_id,preference) DO UPDATE SET value=excluded.value""", (user_id, preference, value))

    @classmethod
    def list_plays(cls, user_id: Optional[str] = None, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with cls._connect() as db:
            if user_id:
                rows = db.execute("SELECT * FROM play_history WHERE user_id=? ORDER BY played_at DESC LIMIT ?", (user_id, limit)).fetchall()
            else:
                rows = db.execute("SELECT * FROM play_history ORDER BY played_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    @classmethod
    def get_show_other_users_controls(cls, user: dict) -> bool:
        if not user.get("is_admin"):
            return False
        with cls._connect() as db:
            row = db.execute("SELECT value FROM user_preferences WHERE user_id=? AND preference='show_other_users_controls'", (cls._user_id(user),)).fetchone()
        return row["value"] == "true" if row else True

    @classmethod
    def set_show_other_users_controls(cls, user: dict, value: bool):
        with cls._connect() as db:
            db.execute("""INSERT INTO user_preferences(user_id,preference,value) VALUES(?, 'show_other_users_controls', ?)
                ON CONFLICT(user_id,preference) DO UPDATE SET value=excluded.value""",
                (cls._user_id(user), "true" if value else "false"))


UserDataService.initialize()
