"""Application settings and durable web UI configuration."""

import json
import os
import re
import tempfile
from typing import Dict, List, Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    DEBUG: bool = False
    MUSIC_OUTPUT_DIR: str = os.path.expanduser("~/Desktop/Music NAS/music")
    JELLYFIN_URL: str = "http://localhost:8096"
    JELLYFIN_API_KEY: Optional[str] = None
    JELLYFIN_USER_ID: Optional[str] = None
    JELLYFIN_SERVER_ID: Optional[str] = None
    LANGUAGE: str = "en"
    # Default accent color: a preset name ("pink", "violet", ...) or "#rrggbb"; users may pick their own.
    ACCENT_COLOR: str = "pink"
    SPOTIFY_CLIENT_ID: Optional[str] = None
    SPOTIFY_CLIENT_SECRET: Optional[str] = None
    SETUP_COMPLETED: bool = False
    # Former or alternate artist names mapped to the name used in the library, e.g. {"tha Supreme": "thasup"}.
    ARTIST_ALIASES: Dict[str, str] = {}

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()


def get_possible_config_paths() -> List[str]:
    configured_path = os.environ.get("MUSIC_SEERR_CONFIG")
    if configured_path:
        return [configured_path]
    app_dir = os.path.dirname(os.path.abspath(__file__))
    backend_dir = os.path.dirname(app_dir)
    project_root = os.path.dirname(backend_dir)
    paths = [
        os.path.join(project_root, "config.json"),
        os.path.join(backend_dir, "config.json"),
        "/app/data/config.json",
    ]
    return list(dict.fromkeys(paths))


def load_webapp_config():
    """Load saved settings, while keeping an explicit music mount authoritative."""
    music_dir_override = os.environ.get("MUSIC_OUTPUT_DIR")
    for path in get_possible_config_paths():
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as config_file:
                data = json.load(config_file)
            if data.get("music_dir"):
                settings.MUSIC_OUTPUT_DIR = data["music_dir"]
            if data.get("jellyfin_url"):
                settings.JELLYFIN_URL = data["jellyfin_url"].rstrip("/")
            if "jellyfin_api_key" in data:
                settings.JELLYFIN_API_KEY = data["jellyfin_api_key"] or None
            if "jellyfin_user_id" in data:
                settings.JELLYFIN_USER_ID = data["jellyfin_user_id"] or None
            if "jellyfin_server_id" in data:
                settings.JELLYFIN_SERVER_ID = data["jellyfin_server_id"] or None
            if isinstance(data.get("artist_aliases"), dict):
                settings.ARTIST_ALIASES = {str(k): str(v) for k, v in data["artist_aliases"].items() if k and v}
            if "language" in data:
                settings.LANGUAGE = data["language"] or "en"
            if valid_accent(data.get("accent_color")):
                settings.ACCENT_COLOR = data["accent_color"]
            settings.SETUP_COMPLETED = bool(
                data.get("setup_completed", data.get("jellyfin_url") and data.get("jellyfin_api_key"))
            )
            print(f"[Config] Loaded web settings from {path}")
            break
        except (OSError, ValueError, TypeError) as exc:
            print(f"[Config] Could not load {path}: {exc}")

    if music_dir_override:
        settings.MUSIC_OUTPUT_DIR = music_dir_override


ACCENT_PRESETS = {"pink", "violet", "blue", "teal", "green", "orange", "gold", "red"}


def valid_accent(value) -> bool:
    """A preset accent name or a #rrggbb color."""
    return isinstance(value, str) and (value in ACCENT_PRESETS or bool(re.fullmatch(r"#[0-9a-fA-F]{6}", value)))


def save_webapp_config(data: dict):
    """Update runtime settings and atomically persist them to the configured data volume."""
    music_dir = os.environ.get("MUSIC_OUTPUT_DIR") or data.get("music_dir", settings.MUSIC_OUTPUT_DIR)
    jellyfin_url = (data.get("jellyfin_url") or settings.JELLYFIN_URL).rstrip("/")
    jellyfin_api_key = data.get("jellyfin_api_key", settings.JELLYFIN_API_KEY) or ""
    jellyfin_user_id = data.get("jellyfin_user_id", settings.JELLYFIN_USER_ID) or ""
    # A new server address invalidates the remembered server identity unless the caller supplies it.
    if "jellyfin_server_id" in data:
        jellyfin_server_id = data.get("jellyfin_server_id") or ""
    elif jellyfin_url != (settings.JELLYFIN_URL or "").rstrip("/"):
        jellyfin_server_id = ""
    else:
        jellyfin_server_id = settings.JELLYFIN_SERVER_ID or ""
    language = data.get("language", settings.LANGUAGE) or "en"
    accent_color = data.get("accent_color") if valid_accent(data.get("accent_color")) else settings.ACCENT_COLOR
    setup_completed = data.get("setup_completed", settings.SETUP_COMPLETED)
    config_dict = {
        "music_dir": music_dir,
        "jellyfin_url": jellyfin_url,
        "jellyfin_api_key": jellyfin_api_key,
        "jellyfin_user_id": jellyfin_user_id,
        "jellyfin_server_id": jellyfin_server_id,
        "language": language,
        "accent_color": accent_color,
        "setup_completed": bool(setup_completed),
        "artist_aliases": dict(settings.ARTIST_ALIASES or {}),
    }

    settings.MUSIC_OUTPUT_DIR = music_dir
    settings.JELLYFIN_URL = jellyfin_url
    settings.JELLYFIN_API_KEY = jellyfin_api_key or None
    settings.JELLYFIN_USER_ID = jellyfin_user_id or None
    settings.JELLYFIN_SERVER_ID = jellyfin_server_id or None
    settings.LANGUAGE = language
    settings.ACCENT_COLOR = accent_color
    settings.SETUP_COMPLETED = bool(setup_completed)

    _write_config(config_dict)


def _write_config(config_dict: dict):
    path = get_possible_config_paths()[0]
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as config_file:
            json.dump(config_dict, config_file, indent=2)
            config_file.flush()
            os.fsync(config_file.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def save_artist_aliases(aliases: dict):
    """Update only `artist_aliases` in the existing config file; fail rather than write a config it cannot read."""
    path = get_possible_config_paths()[0]
    with open(path, "r", encoding="utf-8") as config_file:
        data = json.load(config_file)
    data["artist_aliases"] = dict(aliases)
    _write_config(data)
    settings.ARTIST_ALIASES = dict(aliases)

load_webapp_config()
os.makedirs(settings.MUSIC_OUTPUT_DIR, exist_ok=True)
