"""Encryption at rest for per-user Jellyfin access tokens kept by the backend.

The key comes from the `SONALIS_SECRET` environment variable (any string; preferred, because it lives
outside the data folder) or, when that is not set, from `secret.key` in the data folder, created once with
owner-only permissions. Changing or losing the key only makes stored tokens unreadable: users sign in again.
"""
import base64
import hashlib
import os
import secrets
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

_fernet: Optional[Fernet] = None


def _key_file(data_dir: str) -> str:
    return os.path.join(data_dir, "secret.key")


def _load_key(data_dir: str) -> bytes:
    secret = os.environ.get("SONALIS_SECRET", "").strip()
    if secret:
        return base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
    path = _key_file(data_dir)
    try:
        with open(path, "rb") as handle:
            key = handle.read().strip()
        if key:
            return key
    except FileNotFoundError:
        pass
    os.makedirs(data_dir, mode=0o700, exist_ok=True)
    key = base64.urlsafe_b64encode(secrets.token_bytes(32))
    # O_EXCL: two workers starting together never overwrite each other's key.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        with open(path, "rb") as handle:
            return handle.read().strip()
    with os.fdopen(fd, "wb") as handle:
        handle.write(key)
    return key


def _box(data_dir: str) -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_key(data_dir))
    return _fernet


def encrypt(value: str, data_dir: str) -> str:
    return _box(data_dir).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: Optional[str], data_dir: str) -> Optional[str]:
    if not value:
        return None
    try:
        return _box(data_dir).decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None
