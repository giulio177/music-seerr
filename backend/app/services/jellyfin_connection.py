"""Jellyfin reachability checks and self-repair of the configured server address."""

import asyncio
import os
import socket
import struct
from typing import List, Optional
from urllib.parse import urlparse

import httpx

from app.config import settings, save_webapp_config
from app.services.jellyfin_service import JellyfinService

PROBE_TIMEOUT = 4.0
DEFAULT_PORT = 8096


def _normalize_url(url: Optional[str]) -> str:
    url = (url or "").strip().rstrip("/")
    if url and "://" not in url:
        url = f"http://{url}"
    return url


def _valid_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.hostname)


def _default_gateway() -> Optional[str]:
    """Return the container's default gateway (the Docker host on bridge networks)."""
    try:
        with open("/proc/net/route", "r", encoding="ascii") as route_file:
            for line in route_file.readlines()[1:]:
                fields = line.split()
                if len(fields) > 2 and fields[1] == "00000000":
                    return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    except (OSError, ValueError):
        return None
    return None


def candidate_urls() -> List[str]:
    """Addresses where a Jellyfin server commonly lives relative to this container."""
    configured = _normalize_url(settings.JELLYFIN_URL)
    port = urlparse(configured).port if configured else None
    ports = list(dict.fromkeys([port or DEFAULT_PORT, DEFAULT_PORT]))
    hosts = ["host.docker.internal", "jellyfin", "localhost", "127.0.0.1"]
    gateway = _default_gateway()
    if gateway:
        hosts.insert(1, gateway)
    urls = [_normalize_url(os.environ.get("JELLYFIN_URL"))]
    urls += [f"http://{host}:{p}" for p in ports for host in hosts]
    return [url for url in dict.fromkeys(urls) if url and url != configured]


async def public_info(url: str) -> Optional[dict]:
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT, follow_redirects=False) as client:
            res = await client.get(f"{url}/System/Info/Public")
        if res.status_code == 200:
            data = res.json()
            if isinstance(data, dict) and data.get("Id"):
                return data
    except (httpx.HTTPError, ValueError):
        return None
    return None


async def api_key_server_id(url: str) -> Optional[str]:
    """Return the server ID when the stored API key authenticates against this URL."""
    if not settings.JELLYFIN_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT, follow_redirects=False) as client:
            res = await client.get(f"{url}/System/Info", headers=JellyfinService._headers())
        if res.status_code == 200:
            return res.json().get("Id")
    except (httpx.HTTPError, ValueError):
        return None
    return None


def _describe_error(url: str) -> str:
    host = urlparse(url).hostname or url
    try:
        socket.getaddrinfo(host, None)
    except OSError:
        return "dns"
    return "unreachable"


class JellyfinConnection:
    _lock = asyncio.Lock()

    @classmethod
    async def status(cls) -> dict:
        url = _normalize_url(settings.JELLYFIN_URL)
        result = {
            "configured": bool(url and settings.JELLYFIN_API_KEY),
            "url": url,
            "reachable": False,
            "server_name": "",
            "version": "",
            "error": "",
        }
        if not url:
            result["error"] = "not_configured"
            return result
        info = await public_info(url)
        if not info:
            result["error"] = await asyncio.to_thread(_describe_error, url)
            return result
        if settings.JELLYFIN_SERVER_ID and info.get("Id") != settings.JELLYFIN_SERVER_ID:
            result["error"] = "different_server"
            result["server_name"] = info.get("ServerName", "")
            return result
        result.update(reachable=True, server_name=info.get("ServerName", ""), version=info.get("Version", ""))
        if not settings.JELLYFIN_SERVER_ID and await api_key_server_id(url) == info.get("Id"):
            # Remember the server identity so a later address change can be verified without a sign-in.
            save_webapp_config({"jellyfin_server_id": info["Id"]})
        return result

    @classmethod
    async def _accept(cls, url: str, user_supplied: bool) -> Optional[dict]:
        info = await public_info(url)
        if not info:
            return None
        if settings.JELLYFIN_SERVER_ID:
            # Only the same Jellyfin installation may replace the saved address.
            if info.get("Id") != settings.JELLYFIN_SERVER_ID:
                return None
        elif user_supplied or await api_key_server_id(url) != info.get("Id"):
            # Without a remembered identity, only built-in addresses that accept the saved
            # API key qualify; never send the key to an arbitrary user-supplied address.
            return None
        return info

    @classmethod
    async def repair(cls, requested_url: Optional[str] = None) -> dict:
        """Find the configured Jellyfin server at a new address and persist it."""
        async with cls._lock:
            current = await cls.status()
            if current["reachable"]:
                return {"success": True, "changed": False, "status": current}
            if not current["configured"]:
                return {"success": False, "error": "not_configured", "status": current}

            requested = _normalize_url(requested_url)
            if requested and not _valid_http_url(requested):
                return {"success": False, "error": "invalid_url", "status": current}
            if requested and not settings.JELLYFIN_SERVER_ID:
                return {"success": False, "error": "identity_unknown", "status": current}

            candidates = [requested] if requested else candidate_urls()
            for url in candidates:
                info = await cls._accept(url, user_supplied=bool(requested))
                if info:
                    save_webapp_config({"jellyfin_url": url, "jellyfin_server_id": info["Id"]})
                    print(f"[JellyfinConnection] Jellyfin address updated to {url}")
                    return {"success": True, "changed": True, "status": await cls.status()}
            error = "server_mismatch" if requested and await public_info(requested) else "not_found"
            return {"success": False, "error": error, "status": current}
