"""Persistent per-user application history and server-side sessions."""

import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from typing import Any, Optional


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
                    expires_at REAL NOT NULL
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
            """)
            db.execute("UPDATE download_history SET status='interrupted', updated_at=? WHERE status IN ('pending','downloading','tagging','jellyfin_sync')", (time.time(),))
            db.execute("DELETE FROM app_sessions WHERE expires_at<=?", (time.time(),))

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @classmethod
    def create_session(cls, user: Optional[dict] = None, is_guest: bool = False) -> str:
        now = time.time()
        token = secrets.token_urlsafe(36)
        user = user or {}
        user_id = str(user.get("id") or f"guest:{uuid.uuid4()}")
        name = str(user.get("name") or "Guest")
        is_admin = bool(user.get("is_admin", False)) and not is_guest
        with cls._connect() as db:
            db.execute("DELETE FROM app_sessions WHERE expires_at<=?", (now,))
            db.execute(
                "INSERT INTO app_sessions(token_hash,user_id,display_name,is_admin,is_guest,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                (cls._hash_token(token), user_id, name, int(is_admin), int(is_guest), now, now + cls.SESSION_TTL),
            )
        return token

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
                db.execute("DELETE FROM app_sessions WHERE token_hash=?", (token_hash,))
                return None
            return {
                "id": row["user_id"],
                "name": row["display_name"],
                "is_admin": bool(row["is_admin"]),
                "is_guest": bool(row["is_guest"]),
            }

    @classmethod
    def delete_session(cls, token: Optional[str]):
        if token:
            with cls._connect() as db:
                db.execute("DELETE FROM app_sessions WHERE token_hash=?", (cls._hash_token(token),))

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
    def list_downloads(cls, user_id: Optional[str] = None, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with cls._connect() as db:
            if user_id:
                rows = db.execute("SELECT * FROM download_history WHERE user_id=? ORDER BY created_at DESC LIMIT ?", (user_id, limit)).fetchall()
            else:
                rows = db.execute("SELECT * FROM download_history ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) | {"track": json.loads(row["track_json"])} for row in rows]

    @classmethod
    def record_play(cls, user: dict, track: dict, source: str = "preview") -> bool:
        user_id = cls._user_id(user)
        track_id = str(track.get("id") or "")
        title = str(track.get("title") or "")
        artist = str(track.get("artist") or "")
        if not track_id or not title:
            return False
        now = time.time()
        with cls._connect() as db:
            exists = db.execute("""SELECT id FROM play_history
                WHERE user_id=? AND track_id=? AND played_at>? LIMIT 1""", (user_id, track_id, now - 30)).fetchone()
            if exists:
                return False
            db.execute("""INSERT INTO play_history(user_id,user_name,track_id,title,artist,album,source,played_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (user_id, user.get("name", "Guest"), track_id, title, artist, str(track.get("album") or ""), source, now))
        return True

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
