import os
import re
import time
import asyncio

import httpx
from contextvars import ContextVar, Token
from typing import List, Optional
from app.config import settings
from app.models.schemas import JellyfinPlaylist
from app.services.library_layout import title_key

class JellyfinService:
    _user_scope: ContextVar = ContextVar("jellyfin_user_scope", default="__global_fallback__")

    @classmethod
    def set_request_user_id(cls, user_id: Optional[str]) -> Token:
        return cls._user_scope.set(user_id)

    @classmethod
    def reset_request_user_id(cls, token: Token):
        cls._user_scope.reset(token)

    @classmethod
    def get_scoped_user_id(cls) -> Optional[str]:
        value = cls._user_scope.get()
        return settings.JELLYFIN_USER_ID if value == "__global_fallback__" else value

    @classmethod
    async def get_stream_url(cls, item_id: str, force_mp3: bool = False) -> str:
        """Return direct play immediately; the player retries with MP3 if the browser cannot decode it."""
        if not settings.JELLYFIN_URL or not settings.JELLYFIN_API_KEY:
            return ""

        base = f"{settings.JELLYFIN_URL.rstrip('/')}/Audio/{item_id}"
        if force_mp3:
            return f"{base}/stream.mp3?api_key={settings.JELLYFIN_API_KEY}"
        return f"{base}/stream?static=true&api_key={settings.JELLYFIN_API_KEY}"

    @staticmethod
    def _headers(api_key: Optional[str] = None) -> dict:
        key = api_key or settings.JELLYFIN_API_KEY
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        if key:
            auth_str = f'MediaBrowser Client="MusicSeerr", Device="Server", DeviceId="MusicSeerr-1", Version="1.0.0", Token="{key}"'
            headers["Authorization"] = auth_str
            headers["X-Emby-Authorization"] = auth_str
            headers["X-Emby-Token"] = key
            headers["X-MediaBrowser-Token"] = key
        return headers

    @staticmethod
    def _user_headers(token: str, device_id: str) -> dict:
        """Headers for a call made as the signed-in user (their own Jellyfin token, never sent to the browser)."""
        auth = f'MediaBrowser Client="MusicSeerr", Device="Music Seerr", DeviceId="{device_id}", Version="1.0.0", Token="{token}"'
        return {"Accept": "application/json", "Content-Type": "application/json", "Authorization": auth, "X-Emby-Authorization": auth}

    @classmethod
    async def revoke_user_token(cls, token: str, device_id: str) -> bool:
        """End the Jellyfin session behind a stored user token (logout or expired app session)."""
        if not settings.JELLYFIN_URL or not token:
            return False
        async with httpx.AsyncClient(timeout=8.0) as client:
            try:
                res = await client.post(f"{settings.JELLYFIN_URL.rstrip('/')}/Sessions/Logout",
                                        headers=cls._user_headers(token, device_id))
                return res.status_code in (200, 204)
            except httpx.HTTPError:
                return False

    @classmethod
    async def move_playlist_item(cls, playlist_id: str, item_id: str, new_index: int, credentials: dict) -> str:
        """Move one playlist entry with Jellyfin's own endpoint, as the playlist's owner.
        Returns "ok", "unauthorized" (token no longer valid), or "failed"."""
        if not settings.JELLYFIN_URL:
            return "failed"
        url = f"{settings.JELLYFIN_URL.rstrip('/')}/Playlists/{playlist_id}/Items/{item_id}/Move/{int(new_index)}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                res = await client.post(url, headers=cls._user_headers(credentials["token"], credentials["device_id"]))
            except httpx.HTTPError as e:
                print(f"[JellyfinService] move_playlist_item error: {e}")
                return "failed"
        if res.status_code in (200, 204):
            return "ok"
        return "unauthorized" if res.status_code == 401 else "failed"

    @classmethod
    async def get_music_library_paths(cls, jellyfin_url: Optional[str] = None, api_key: Optional[str] = None) -> List[str]:
        """Rileva i percorsi fisici della libreria musicale configurata su Jellyfin"""
        effective_url = (jellyfin_url or settings.JELLYFIN_URL or "").rstrip('/')
        effective_key = api_key or settings.JELLYFIN_API_KEY
        if not effective_key or not effective_url:
            return []

        headers = cls._headers(api_key=effective_key)
        base_url = effective_url
        paths: List[str] = []

        async with httpx.AsyncClient(timeout=4.0) as client:
            # 1. Tentativo con endpoint admin /Library/VirtualFolders
            try:
                res = await client.get(f"{base_url}/Library/VirtualFolders", headers=headers)
                if res.status_code == 200:
                    for lib in res.json():
                        if (lib.get("CollectionType") or "").lower() == "music":
                            for loc in lib.get("Locations", []):
                                if loc and loc not in paths:
                                    paths.append(loc)
            except Exception as e:
                print(f"[JellyfinService] VirtualFolders error: {e}")

            # 2. Se non disponibile (es. permessi limitati o token non-admin), ispeziona le tracce audio esistenti
            if not paths:
                try:
                    items_res = await client.get(
                        f"{base_url}/Items?recursive=true&includeItemTypes=Audio&limit=5&fields=Path",
                        headers=headers
                    )
                    if items_res.status_code == 200:
                        for item in items_res.json().get("Items", []):
                            track_path = item.get("Path")
                            if track_path:
                                parts = track_path.split("/")
                                if "Music" in parts:
                                    idx = parts.index("Music")
                                    root = "/".join(parts[:idx+1])
                                    if root and root not in paths:
                                        paths.append(root)
                                elif len(parts) >= 3:
                                    root = "/".join(parts[:-3])
                                    if root and root not in paths:
                                        paths.append(root)
                except Exception as e:
                    print(f"[JellyfinService] Item path inspection error: {e}")

        return paths

    @classmethod
    async def authenticate_user(cls, username: str, password: str, jellyfin_url: Optional[str] = None,
                                device_id: str = "MusicSeerr-Web") -> dict:
        """Autentica l'utente con username e password Jellyfin tramite /Users/AuthenticateByName.
        Jellyfin revokes earlier tokens of the same user and device ID, so each app session uses its own."""
        url = (jellyfin_url or settings.JELLYFIN_URL).rstrip('/')
        if not url:
            return {"success": False, "message": "Jellyfin URL is not configured."}

        auth_endpoint = f"{url}/Users/AuthenticateByName"
        authorization = f'MediaBrowser Client="MusicSeerr", Device="Music Seerr", DeviceId="{device_id}", Version="1.0.0"'
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": authorization,
            "X-Emby-Authorization": authorization
        }
        payload = {
            "Username": username,
            "Pw": password
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                res = await client.post(auth_endpoint, json=payload, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    user = data.get("User", {})
                    token = data.get("AccessToken", "")
                    user_info = {
                        "id": user.get("Id"),
                        "name": user.get("Name"),
                        "is_admin": user.get("Policy", {}).get("IsAdministrator", False),
                        "has_image": bool(user.get("PrimaryImageTag")),
                        "image_url": f"/api/jellyfin/user-image/{user.get('Id')}"
                    }
                    return {
                        "success": True,
                        "token": token,
                        "user": user_info
                    }
                elif res.status_code in (400, 401):
                    try:
                        error_payload = res.json()
                        server_message = error_payload.get("Message") or error_payload.get("message")
                    except (ValueError, AttributeError):
                        server_message = None
                    return {
                        "success": False,
                        "message": server_message or "Incorrect username or password."
                    }
                else:
                    return {
                        "success": False,
                        "message": f"Jellyfin authentication failed: HTTP {res.status_code}"
                    }
            except Exception as e:
                return {
                    "success": False,
                    "message": f"Could not contact the Jellyfin server: {str(e)}"
                }

    @classmethod
    async def get_users(cls, jellyfin_url: Optional[str] = None, api_key: Optional[str] = None) -> List[dict]:
        """Restituisce l'elenco degli utenti registrati su Jellyfin"""
        url = (jellyfin_url or settings.JELLYFIN_URL).rstrip('/')
        key = api_key or settings.JELLYFIN_API_KEY
        if not url:
            return []

        users_list = []
        async with httpx.AsyncClient(timeout=6.0) as client:
            if key:
                try:
                    headers = cls._headers(api_key=key)
                    res = await client.get(f"{url}/Users", headers=headers)
                    if res.status_code == 200:
                        users_list = res.json()
                except Exception as e:
                    print(f"[JellyfinService] Error getting users with API key: {e}")

            if not users_list:
                try:
                    res = await client.get(f"{url}/Users/Public")
                    if res.status_code == 200:
                        users_list = res.json()
                except Exception as e:
                    print(f"[JellyfinService] Error getting public users: {e}")

        result = []
        for u in users_list:
            result.append({
                "id": u.get("Id"),
                "name": u.get("Name"),
                "is_admin": u.get("Policy", {}).get("IsAdministrator", False),
                "has_image": bool(u.get("PrimaryImageTag")),
                "image_url": f"/api/jellyfin/user-image/{u.get('Id')}"
            })
        return result

    @classmethod
    async def get_playlists(cls, user_id: Optional[str] = None) -> List[JellyfinPlaylist]:
        """Recupera le playlist da Jellyfin se configurato"""
        if not settings.JELLYFIN_API_KEY or not settings.JELLYFIN_URL:
            return []

        uid = user_id if user_id is not None else cls.get_scoped_user_id()
        if not uid:
            return []
        url = f"{settings.JELLYFIN_URL.rstrip('/')}/Users/{uid}/Items" if uid else f"{settings.JELLYFIN_URL.rstrip('/')}/Items"
        params = {
            "includeItemTypes": "Playlist",
            "recursive": "true",
            "sortBy": "SortName"
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                res = await client.get(url, params=params, headers=cls._headers())
                if res.status_code == 200:
                    data = res.json()
                    items = data.get("Items", [])
                    return [
                        JellyfinPlaylist(
                            id=item.get("Id"),
                            name=item.get("Name"),
                            track_count=item.get("ChildCount", 0),
                            cover_url=f"/api/jellyfin/image/{item.get('Id')}",
                            owner_user_id=item.get("OwnerUserId"),
                            owner_name=item.get("OwnerUserName")
                        )
                        for item in items
                    ]
            except Exception as e:
                print(f"[JellyfinService] Errore get_playlists: {e}")
        return []

    _owner_map_cache: Optional[tuple] = None
    _owner_map_lock: Optional[asyncio.Lock] = None
    OWNER_MAP_TTL = 60

    @classmethod
    async def _get_playlist_share_ids(cls, client: httpx.AsyncClient, playlist_id: str) -> Optional[set]:
        """Returns the user IDs a playlist is shared with, or None when Jellyfin does not expose them."""
        try:
            res = await client.get(
                f"{settings.JELLYFIN_URL.rstrip('/')}/Playlists/{playlist_id}",
                headers=cls._headers(),
            )
            if res.status_code != 200:
                return None
            data = res.json()
            shares = data.get("Shares")
            if not isinstance(shares, list):
                return None
            return {str(share.get("UserId")).replace("-", "").lower() for share in shares if share.get("UserId")}
        except Exception:
            return None

    PLAYLIST_XML_MAX_BYTES = 16 * 1024 * 1024
    _OWNER_XML_RE = re.compile(r"<OwnerUserId>\s*([0-9a-fA-F-]{32,36})\s*</OwnerUserId>")

    @classmethod
    async def _get_playlist_owner_from_xml(cls, client: httpx.AsyncClient, playlist_id: str, user_id: str) -> Optional[str]:
        """Reads OwnerUserId from Jellyfin's playlist.xml through the optional read-only mount."""
        root = os.environ.get("JELLYFIN_PLAYLISTS_DIR", "/jellyfin-playlists")
        if not root or not os.path.isdir(root):
            return None
        try:
            res = await client.get(
                f"{settings.JELLYFIN_URL.rstrip('/')}/Users/{user_id}/Items/{playlist_id}",
                headers=cls._headers(),
            )
            if res.status_code != 200:
                return None
            folder = os.path.basename(str(res.json().get("Path") or "").rstrip("/"))
            if not folder or folder in (".", ".."):
                return None
            root_real = os.path.realpath(root)
            xml_path = os.path.realpath(os.path.join(root_real, folder, "playlist.xml"))
            if os.path.commonpath([root_real, xml_path]) != root_real or not os.path.isfile(xml_path):
                return None
            if os.path.getsize(xml_path) > cls.PLAYLIST_XML_MAX_BYTES:
                return None
            with open(xml_path, "r", encoding="utf-8", errors="replace") as handle:
                match = cls._OWNER_XML_RE.search(handle.read())
            return match.group(1).replace("-", "").lower() if match else None
        except Exception as e:
            print(f"[JellyfinService] Could not read playlist owner for {playlist_id}: {e}")
            return None

    @classmethod
    async def get_playlist_owner_map(cls, playlist_ids: Optional[set] = None) -> dict:
        """Maps playlist IDs to owner user IDs.

        Jellyfin item listings omit OwnerUserId. The owner is read from
        Jellyfin's playlist.xml when the playlists folder is mounted;
        otherwise it is inferred: a playlist owner always sees its own
        playlist, so when exactly one account sees it (after excluding
        accounts it is shared with), that account is the owner. Ambiguous
        playlists stay unresolved.
        """
        if not settings.JELLYFIN_API_KEY or not settings.JELLYFIN_URL:
            return {}
        if cls._owner_map_lock is None:
            cls._owner_map_lock = asyncio.Lock()
        async with cls._owner_map_lock:
            cached = cls._owner_map_cache
            if (
                cached
                and time.monotonic() - cached[0] < cls.OWNER_MAP_TTL
                and not (playlist_ids or set()) - cached[2]
            ):
                return cached[1]
            users = [user for user in await cls.get_users() if user.get("id")]
            results = await asyncio.gather(*(cls.get_playlists(user_id=user["id"]) for user in users))
            owners: dict = {}
            visible_to: dict = {}
            for user, playlists in zip(users, results):
                for playlist in playlists:
                    if playlist.owner_user_id:
                        owners[playlist.id] = playlist.owner_user_id
                    visible_to.setdefault(playlist.id, set()).add(user["id"])
            unresolved = [pid for pid in visible_to if pid not in owners]
            known_ids = {user["id"].replace("-", "").lower(): user["id"] for user in users}
            async with httpx.AsyncClient(timeout=6.0) as client:
                xml_owners = await asyncio.gather(*(
                    cls._get_playlist_owner_from_xml(client, pid, next(iter(visible_to[pid])))
                    for pid in unresolved
                ))
                for playlist_id, xml_owner in zip(unresolved, xml_owners):
                    if xml_owner and xml_owner in known_ids:
                        owners[playlist_id] = known_ids[xml_owner]
                unresolved = [pid for pid in unresolved if pid not in owners]
                share_results = await asyncio.gather(*(cls._get_playlist_share_ids(client, pid) for pid in unresolved))
            for playlist_id, share_ids in zip(unresolved, share_results):
                candidates = {
                    uid for uid in visible_to[playlist_id]
                    if not share_ids or uid.replace("-", "").lower() not in share_ids
                }
                if len(candidates) == 1:
                    owners[playlist_id] = next(iter(candidates))
            cls._owner_map_cache = (time.monotonic(), owners, set(visible_to))
            return owners

    @classmethod
    async def refresh_library(cls) -> bool:
        """Forza la scansione della libreria su Jellyfin per rilevare nuovi file"""
        if not settings.JELLYFIN_API_KEY or not settings.JELLYFIN_URL:
            return False
            
        url = f"{settings.JELLYFIN_URL.rstrip('/')}/Library/Refresh"
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                res = await client.post(url, headers=cls._headers())
                return res.status_code in [200, 204]
            except Exception as e:
                print(f"[JellyfinService] Errore refresh_library: {e}")
                return False

    @classmethod
    async def notify_new_file(cls, local_path: str) -> bool:
        """Ask Jellyfin to scan just this file (much faster than a full library scan on large libraries).
        The music-root path is translated to Jellyfin's own library path."""
        if not settings.JELLYFIN_API_KEY or not settings.JELLYFIN_URL:
            return False
        music_root = os.path.realpath(settings.MUSIC_OUTPUT_DIR)
        relative = os.path.relpath(os.path.realpath(local_path), music_root).replace(os.sep, "/")
        if relative.startswith(".."):
            return False
        roots = await cls.get_music_library_paths() or [music_root]
        updates = [{"Path": f"{root.rstrip('/')}/{relative}", "UpdateType": "Created"} for root in roots]
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                res = await client.post(f"{settings.JELLYFIN_URL.rstrip('/')}/Library/Media/Updated",
                                        json={"Updates": updates}, headers=cls._headers())
                return res.status_code in (200, 204)
            except Exception as e:
                print(f"[JellyfinService] notify_new_file error: {e}")
                return False

    _root_refreshed_at: dict = {}

    @classmethod
    async def scan_file(cls, local_path: str, wait: float = 20.0, changed: bool = False) -> Optional[str]:
        """Make Jellyfin index one file now and return its item ID once it is in the library.

        Refreshes the deepest library folder Jellyfin already knows (the album, else the artist), which
        Jellyfin processes in about a second. `/Library/Media/Updated` instead waits for the library
        monitor delay (60 s by default, restarted by every change) and is lost if Jellyfin restarts.
        `changed` refreshes the folder of a file Jellyfin already has (replaced audio, new lyrics).
        Returns None when the file could not be confirmed within `wait` seconds."""
        if not settings.JELLYFIN_API_KEY or not settings.JELLYFIN_URL:
            return None
        music_root = os.path.realpath(settings.MUSIC_OUTPUT_DIR)
        relative = os.path.relpath(os.path.realpath(local_path), music_root).replace(os.sep, "/")
        if relative.startswith("..") or relative == ".":
            return None
        return await cls.scan_relative(relative, wait, changed)

    @classmethod
    async def scan_relative(cls, relative: str, wait: float = 20.0, changed: bool = False) -> Optional[str]:
        base = settings.JELLYFIN_URL.rstrip("/")
        headers = cls._headers()
        parts = [part for part in relative.split("/") if part]
        deadline = time.monotonic() + wait
        async with httpx.AsyncClient(timeout=10.0) as client:

            async def child(parent_id: str, path: str) -> Optional[str]:
                res = await client.get(f"{base}/Items", params={"ParentId": parent_id, "Fields": "Path"}, headers=headers)
                if res.status_code != 200:
                    return None
                return next((item.get("Id") for item in res.json().get("Items", []) if item.get("Path") == path), None)

            async def locate(library_id: str, location: str) -> tuple:
                """The deepest known folder on the way to the file, its depth, and the file's item ID if indexed."""
                current, depth = library_id, 0
                for index in range(len(parts)):
                    found = await child(current, f"{location}/{'/'.join(parts[:index + 1])}")
                    if not found:
                        break
                    if index == len(parts) - 1:
                        return current, depth, found
                    current, depth = found, index + 1
                return current, depth, None

            async def refresh(folder: str, root: bool, status: str):
                if root:
                    # Refreshing the library root rescans the whole library in the background (the new
                    # artist folder appears within seconds), so never stack it on a running or recent scan.
                    if status == "Active" or time.monotonic() - cls._root_refreshed_at.get(folder, -1e9) < 120:
                        return
                    cls._root_refreshed_at[folder] = time.monotonic()
                await client.post(f"{base}/Items/{folder}/Refresh", headers=headers, params={
                    "Recursive": "true", "MetadataRefreshMode": "Default", "ImageRefreshMode": "Default",
                    "ReplaceAllMetadata": "false", "ReplaceAllImages": "false"})

            try:
                res = await client.get(f"{base}/Library/VirtualFolders", headers=headers)
                if res.status_code != 200:
                    return None
                libraries = [(lib.get("ItemId"), loc.rstrip("/"), lib.get("RefreshStatus") or "") for lib in res.json()
                             if (lib.get("CollectionType") or "").lower() == "music" and lib.get("ItemId")
                             for loc in lib.get("Locations", []) if loc]
                if not libraries:
                    return None
                # The library holding the file's artist folder; with a brand-new artist, the first one.
                located = [(lib, await locate(lib[0], lib[1])) for lib in libraries]
                (library_id, location, status), (folder, depth, item_id) = max(located, key=lambda entry: entry[1][1])
                if item_id:
                    if changed:
                        await refresh(folder, depth == 0, status)
                    return item_id
                refreshed = None
                while not item_id:
                    if folder != refreshed:
                        await refresh(folder, depth == 0, status)
                        refreshed = folder
                    if time.monotonic() >= deadline:
                        return None
                    await asyncio.sleep(1.0)
                    folder, depth, item_id = await locate(library_id, location)
                return item_id
            except (httpx.HTTPError, ValueError) as e:
                print(f"[JellyfinService] scan_file error for {relative}: {e}")
                return None

    @classmethod
    async def find_track_id(cls, title: str, artist: str, user_id: Optional[str] = None, strict: bool = False) -> Optional[str]:
        """Cerca l'ID Jellyfin del brano appena scansionato"""
        if not settings.JELLYFIN_API_KEY or not settings.JELLYFIN_URL:
            return None

        scoped_user_id = user_id if user_id is not None else cls.get_scoped_user_id()
        url = f"{settings.JELLYFIN_URL.rstrip('/')}/Users/{scoped_user_id}/Items" if scoped_user_id else f"{settings.JELLYFIN_URL.rstrip('/')}/Items"
        params = {
            "searchTerm": title,
            "includeItemTypes": "Audio",
            "recursive": "true",
            "limit": 20
        }
        wanted = title_key(title)

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                res = await client.get(url, params=params, headers=cls._headers())
                if res.status_code == 200:
                    # Only exact title keys count: "Believer (Instrumental)" never stands in for "Believer".
                    items = [item for item in res.json().get("Items", []) if title_key(item.get("Name", "")) == wanted]
                    for item in items:
                        item_artists = [a.lower() for a in item.get("Artists", [])]
                        if not artist or any(artist.lower() in a for a in item_artists) or artist.lower() in item.get("Name", "").lower():
                            return item.get("Id")
                    if items and not strict:
                        return items[0].get("Id")
            except Exception as e:
                print(f"[JellyfinService] Errore find_track_id: {e}")
        return None

    @classmethod
    async def add_to_playlist(cls, playlist_id: str, item_id: str, user_id: Optional[str] = None) -> bool:
        """Aggiunge un brano alla playlist specificata"""
        if not settings.JELLYFIN_API_KEY or not settings.JELLYFIN_URL:
            return False

        url = f"{settings.JELLYFIN_URL.rstrip('/')}/Playlists/{playlist_id}/Items"
        params = {"ids": item_id}
        if user_id:
            params["userId"] = user_id
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                res = await client.post(url, params=params, headers=cls._headers())
                return res.status_code in [200, 204]
            except Exception as e:
                print(f"[JellyfinService] Errore add_to_playlist: {e}")
                return False

    @classmethod
    async def remove_from_playlist(cls, playlist_id: str, entry_id: str, user_id: Optional[str] = None) -> bool:
        """Remove one playlist entry. Jellyfin expects the playlist entry ID, not the audio item ID."""
        if not settings.JELLYFIN_API_KEY or not settings.JELLYFIN_URL:
            return False
        url = f"{settings.JELLYFIN_URL.rstrip('/')}/Playlists/{playlist_id}/Items"
        params = {"entryIds": entry_id}
        if user_id:
            params["userId"] = user_id
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                res = await client.delete(url, params=params, headers=cls._headers())
                return res.status_code in (200, 204)
            except Exception as e:
                print(f"[JellyfinService] Error removing playlist entry: {e}")
                return False

    _playlist_locks: dict = {}

    @classmethod
    async def playlist_entry_ids(cls, playlist_id: str, user_id: Optional[str] = None) -> Optional[List[str]]:
        """The playlist's audio item IDs in playlist order (duplicates included)."""
        if not settings.JELLYFIN_API_KEY or not settings.JELLYFIN_URL:
            return None
        params = {"userId": user_id} if user_id else {}
        async with httpx.AsyncClient(timeout=20.0) as client:
            try:
                res = await client.get(f"{settings.JELLYFIN_URL.rstrip('/')}/Playlists/{playlist_id}/Items",
                                       params=params, headers=cls._headers())
                if res.status_code != 200:
                    return None
                return [item.get("Id") for item in res.json().get("Items", []) if item.get("Id")]
            except (httpx.HTTPError, ValueError) as e:
                print(f"[JellyfinService] playlist_entry_ids error: {e}")
                return None

    @staticmethod
    def single_move(base_ids: List[str], item_ids: List[str]) -> Optional[tuple]:
        """(from, to) when `item_ids` is `base_ids` with exactly one entry moved, else None."""
        if len(base_ids) != len(item_ids) or base_ids == item_ids:
            return None
        first = next(i for i in range(len(base_ids)) if base_ids[i] != item_ids[i])
        last = next(i for i in range(len(base_ids) - 1, -1, -1) if base_ids[i] != item_ids[i])
        if base_ids[first + 1:last + 1] == item_ids[first:last] and base_ids[first] == item_ids[last]:
            return first, last
        if base_ids[first:last] == item_ids[first + 1:last + 1] and base_ids[last] == item_ids[first]:
            return last, first
        return None

    @classmethod
    async def reorder_playlist(cls, playlist_id: str, base_ids: List[str], item_ids: List[str],
                               user_id: Optional[str] = None, credentials: Optional[dict] = None) -> dict:
        """Save a new playlist order. A single moved song (that appears once) uses Jellyfin's own move endpoint
        with the owner's token; anything else, or no usable token, rewrites the playlist (`rewrite_playlist`).
        Returns {"status": ok|changed|invalid|failed, "method": move|rewrite|none, "token_rejected": bool}."""
        move = cls.single_move(list(base_ids), list(item_ids))
        token_rejected = False
        if move and credentials and list(base_ids).count(base_ids[move[0]]) == 1:
            lock = cls._playlist_locks.setdefault(playlist_id, asyncio.Lock())
            async with lock:
                current = await cls.playlist_entry_ids(playlist_id, user_id)
                if current is not None and current != list(base_ids):
                    return {"status": "changed", "method": "none", "token_rejected": False}
                if current is not None:
                    result = await cls.move_playlist_item(playlist_id, base_ids[move[0]], move[1], credentials)
                    if result == "ok":
                        after = await cls.playlist_entry_ids(playlist_id, user_id)
                        if after == list(item_ids):
                            return {"status": "ok", "method": "move", "token_rejected": False}
                        print(f"[JellyfinService] Move left playlist {playlist_id} in an unexpected order; rewriting it")
                        base_ids = after or base_ids
                    token_rejected = result == "unauthorized"
        status = await cls.rewrite_playlist(playlist_id, base_ids, item_ids, user_id)
        return {"status": status, "method": "rewrite", "token_rejected": token_rejected}

    @classmethod
    async def rewrite_playlist(cls, playlist_id: str, base_ids: List[str], item_ids: List[str],
                               user_id: Optional[str] = None) -> str:
        """Replace the playlist's entries with `item_ids` (a reordering of the current entries, possibly minus some).

        Jellyfin's own move endpoint needs a user token, and since Jellyfin 12 an entry ID equals its item ID, so
        removing one copy of a repeated song removes all of them. Rewriting (remove every entry, then add the new
        list in order) works with the server API key and keeps repeated songs. Returns "ok", "changed" (the
        playlist no longer matches `base_ids`), "invalid" (`item_ids` is not drawn from it), or "failed"."""
        from collections import Counter
        base = f"{settings.JELLYFIN_URL.rstrip('/')}/Playlists/{playlist_id}/Items"
        params = {"userId": user_id} if user_id else {}
        lock = cls._playlist_locks.setdefault(playlist_id, asyncio.Lock())
        async with lock:
            current = await cls.playlist_entry_ids(playlist_id, user_id)
            if current is None:
                return "failed"
            if current != list(base_ids):
                return "changed"
            if Counter(item_ids) - Counter(current):
                return "invalid"
            if list(item_ids) == current:
                return "ok"
            chunk = 50
            async with httpx.AsyncClient(timeout=30.0) as client:

                async def add(ids: List[str]) -> bool:
                    for start in range(0, len(ids), chunk):
                        res = await client.post(base, params={**params, "ids": ",".join(ids[start:start + chunk])},
                                                headers=cls._headers())
                        if res.status_code not in (200, 204):
                            return False
                    return True

                try:
                    distinct = list(dict.fromkeys(current))
                    for start in range(0, len(distinct), chunk):
                        res = await client.delete(base, params={**params, "entryIds": ",".join(distinct[start:start + chunk])},
                                                  headers=cls._headers())
                        if res.status_code not in (200, 204):
                            # Nothing (or only part) was removed: put back whatever is missing, in order.
                            left = await cls.playlist_entry_ids(playlist_id, user_id) or []
                            if left != current:
                                await client.delete(base, params={**params, "entryIds": ",".join(dict.fromkeys(left))},
                                                    headers=cls._headers())
                                await add(current)
                            return "failed"
                    if await add(list(item_ids)):
                        return "ok"
                    # Restore the original list rather than leave the playlist half written.
                    await client.delete(base, params={**params, "entryIds": ",".join(dict.fromkeys(current + list(item_ids)))},
                                        headers=cls._headers())
                    await add(current)
                    return "failed"
                except httpx.HTTPError as e:
                    print(f"[JellyfinService] rewrite_playlist error for {playlist_id}: {e}")
                    return "failed"

    @classmethod
    async def refresh_replaced_item(cls, item_id: str, new_length: float, wait: float = 90.0) -> bool:
        """Make Jellyfin re-read a file whose audio was replaced at the same path (same item, so playlists,
        favorites and play counts stay) and wait until it reports the new length. False on timeout."""
        if not settings.JELLYFIN_API_KEY or not settings.JELLYFIN_URL or not re.fullmatch(r"[0-9a-f]{32}", item_id or ""):
            return False
        base, headers = settings.JELLYFIN_URL.rstrip("/"), cls._headers()
        refresh = {"MetadataRefreshMode": "FullRefresh", "ImageRefreshMode": "Default",
                   "ReplaceAllMetadata": "false", "ReplaceAllImages": "false"}
        deadline = time.monotonic() + wait
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                res = await client.get(f"{base}/Items", params={"Ids": item_id}, headers=headers)
                items = res.json().get("Items", []) if res.status_code == 200 else []
                old_length = (items[0].get("RunTimeTicks") or 0) / 10_000_000 if items else 0
                await client.post(f"{base}/Items/{item_id}/Refresh", params=refresh, headers=headers)
                # Same length as before: nothing to watch for, give the probe a moment.
                if not new_length or abs(new_length - old_length) < 1.5:
                    await asyncio.sleep(4)
                    return True
                attempt = 0
                while time.monotonic() < deadline:
                    await asyncio.sleep(1.5)
                    attempt += 1
                    res = await client.get(f"{base}/Items", params={"Ids": item_id}, headers=headers)
                    items = res.json().get("Items", []) if res.status_code == 200 else []
                    length = (items[0].get("RunTimeTicks") or 0) / 10_000_000 if items else 0
                    if items and abs(length - new_length) < 1.5:
                        return True
                    if attempt % 10 == 0:
                        await client.post(f"{base}/Items/{item_id}/Refresh", params=refresh, headers=headers)
            except (httpx.HTTPError, ValueError) as e:
                print(f"[JellyfinService] refresh_replaced_item error for {item_id}: {e}")
        return False

    @classmethod
    async def playlists_containing(cls, item_id: str) -> List[tuple]:
        """(playlist ID, owner user ID, entry IDs) of every user's playlist that holds the item."""
        found, seen = [], set()
        for user in await cls.get_users():
            for playlist in await cls.get_playlists(user["id"]):
                if playlist.id in seen:
                    continue
                seen.add(playlist.id)
                owner = playlist.owner_user_id or user["id"]
                entries = await cls.playlist_entry_ids(playlist.id, owner)
                if entries and item_id in entries:
                    found.append((playlist.id, owner, entries))
        return found

    @classmethod
    async def swap_playlist_item(cls, playlist_id: str, entries: List[str], old_id: str, new_id: str,
                                 user_id: Optional[str] = None) -> bool:
        """Put `new_id` where `old_id` was in the playlist (entries read before the old file went away)."""
        target = [new_id if entry == old_id else entry for entry in entries]
        base = f"{settings.JELLYFIN_URL.rstrip('/')}/Playlists/{playlist_id}/Items"
        params = {"userId": user_id} if user_id else {}
        lock = cls._playlist_locks.setdefault(playlist_id, asyncio.Lock())
        async with lock, httpx.AsyncClient(timeout=30.0) as client:
            try:
                current = list(dict.fromkeys(await cls.playlist_entry_ids(playlist_id, user_id) or []))
                for start in range(0, len(current), 50):
                    await client.delete(base, params={**params, "entryIds": ",".join(current[start:start + 50])},
                                        headers=cls._headers())
                for start in range(0, len(target), 50):
                    res = await client.post(base, params={**params, "ids": ",".join(target[start:start + 50])}, headers=cls._headers())
                    if res.status_code not in (200, 204):
                        return False
                return True
            except httpx.HTTPError as e:
                print(f"[JellyfinService] swap_playlist_item error for {playlist_id}: {e}")
                return False

    @classmethod
    async def set_favorite(cls, item_id: str, user_id: str, favorite: bool) -> bool:
        """Set a Jellyfin favorite for the verified user."""
        if not settings.JELLYFIN_API_KEY or not settings.JELLYFIN_URL:
            return False
        url = f"{settings.JELLYFIN_URL.rstrip('/')}/Users/{user_id}/FavoriteItems/{item_id}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                accessible = await client.get(
                    f"{settings.JELLYFIN_URL.rstrip('/')}/Users/{user_id}/Items/{item_id}",
                    headers=cls._headers()
                )
                if accessible.status_code != 200:
                    return False
                res = await (client.post if favorite else client.delete)(url, headers=cls._headers())
                return res.status_code in (200, 204)
            except Exception as e:
                print(f"[JellyfinService] Error updating favorite: {e}")
                return False

    @staticmethod
    def artist_credit(artists: List[str], album_artist: Optional[str] = None) -> str:
        """Credit line such as "Main feat. Guest A, Guest B"; the album artist leads when it is credited."""
        names = [name for name in artists or [] if name]
        if not names:
            return album_artist or ""
        if album_artist and album_artist in names:
            names = [album_artist] + [name for name in names if name != album_artist]
        if len(names) == 1:
            return names[0]
        return f"{names[0]} feat. {', '.join(names[1:])}"

    @staticmethod
    def _format_track_item(item: dict) -> dict:
        item_id = item.get("Id")
        ticks = item.get("RunTimeTicks", 0) or 0
        total_sec = ticks // 10000000
        mins = total_sec // 60
        secs = total_sec % 60
        duration_formatted = f"{mins}:{secs:02d}"

        artists = item.get("Artists", [])
        artist_name = JellyfinService.artist_credit(artists, item.get("AlbumArtist")) or "Unknown artist"

        artist_id = None
        artist_items = item.get("ArtistItems", [])
        if artist_items and isinstance(artist_items, list) and len(artist_items) > 0:
            artist_id = artist_items[0].get("Id")
        elif item.get("AlbumArtists") and isinstance(item.get("AlbumArtists"), list) and len(item.get("AlbumArtists")) > 0:
            artist_id = item.get("AlbumArtists")[0].get("Id")

        album_id = item.get("AlbumId") or (item.get("ParentId") if item.get("Type") == "Audio" else None)

        return {
            "id": item_id,
            "title": item.get("Name", "Untitled track"),
            "artist": artist_name,
            "artists": artists,
            "artist_id": artist_id,
            "album": item.get("Album", ""),
            "album_id": album_id,
            "duration": duration_formatted,
            "duration_sec": total_sec,
            "index_number": item.get("IndexNumber"),
            "disc_number": item.get("ParentIndexNumber"),
            "has_lyrics": item.get("HasLyrics", False),
            "is_favorite": bool((item.get("UserData") or {}).get("IsFavorite")),
            "playlist_entry_id": item.get("PlaylistItemId"),
            "path": item.get("Path", ""),
            "stream_url": f"/api/jellyfin/stream/{item_id}",
            "cover_url": f"/api/jellyfin/image/{item_id}"
        }

    @classmethod
    async def get_library_tracks(
        cls,
        q: Optional[str] = None,
        playlist_id: Optional[str] = None,
        has_lyrics: Optional[bool] = None,
        limit: int = 50,
        offset: int = 0,
        user_id: Optional[str] = None
    ) -> dict:
        import asyncio
        if not settings.JELLYFIN_API_KEY or not settings.JELLYFIN_URL:
            return {"total": 0, "tracks": []}

        user_id = user_id if user_id is not None else cls.get_scoped_user_id()
        fields = "HasLyrics,UserData,Path,Artists,Album,RunTimeTicks,PrimaryImageAspectRatio,AlbumId,ArtistItems,AlbumArtists,ParentId"

        async with httpx.AsyncClient(timeout=10.0) as client:
            headers = cls._headers()

            # Se è specificata una playlist
            if playlist_id:
                url = f"{settings.JELLYFIN_URL.rstrip('/')}/Playlists/{playlist_id}/Items"
                params = {
                    "userId": user_id,
                    "fields": fields,
                    "limit": limit,
                    "startIndex": offset
                }
                res = await client.get(url, headers=headers, params=params)
                if res.status_code == 200:
                    data = res.json()
                    items = data.get("Items", [])
                    total = data.get("TotalRecordCount", len(items))
                    tracks = [cls._format_track_item(it) for it in items]
                    if has_lyrics is not None:
                        tracks = [t for t in tracks if t["has_lyrics"] == has_lyrics]
                    return {"total": total, "tracks": tracks}
                return {"total": 0, "tracks": []}

            # Se c'è una ricerca testuale: cerca sia per titolo che per artista in parallelo
            if q and q.strip():
                query = q.strip()
                params_title = {
                    "searchTerm": query,
                    "includeItemTypes": "Audio",
                    "recursive": "true",
                    "fields": fields,
                    "limit": limit
                }
                params_artist = {
                    "artists": query,
                    "includeItemTypes": "Audio",
                    "recursive": "true",
                    "fields": fields,
                    "limit": limit
                }
                url = f"{settings.JELLYFIN_URL.rstrip('/')}/Users/{user_id}/Items" if user_id else f"{settings.JELLYFIN_URL.rstrip('/')}/Items"

                res_title, res_artist = await asyncio.gather(
                    client.get(url, headers=headers, params=params_title),
                    client.get(url, headers=headers, params=params_artist),
                    return_exceptions=True
                )

                seen_ids = set()
                combined = []
                for r in [res_title, res_artist]:
                    if isinstance(r, httpx.Response) and r.status_code == 200:
                        for it in r.json().get("Items", []):
                            if it.get("Id") not in seen_ids:
                                seen_ids.add(it.get("Id"))
                                combined.append(cls._format_track_item(it))

                if has_lyrics is not None:
                    combined = [t for t in combined if t["has_lyrics"] == has_lyrics]

                return {"total": len(combined), "tracks": combined[:limit]}

            # Vista di default con paginazione
            url = f"{settings.JELLYFIN_URL.rstrip('/')}/Users/{user_id}/Items" if user_id else f"{settings.JELLYFIN_URL.rstrip('/')}/Items"
            params = {
                "includeItemTypes": "Audio",
                "recursive": "true",
                "sortBy": "SortName",
                "fields": fields,
                "limit": limit,
                "startIndex": offset
            }
            if has_lyrics is not None:
                params["hasLyrics"] = str(has_lyrics).lower()

            res = await client.get(url, headers=headers, params=params)
            if res.status_code == 200:
                data = res.json()
                items = data.get("Items", [])
                total = data.get("TotalRecordCount", len(items))
                tracks = [cls._format_track_item(it) for it in items]
                if has_lyrics is not None:
                    tracks = [t for t in tracks if t["has_lyrics"] == has_lyrics]
                return {"total": total, "tracks": tracks}

            return {"total": 0, "tracks": []}

    @classmethod
    async def get_albums(cls, q: Optional[str] = None, limit: int = 150, offset: int = 0) -> dict:
        if not settings.JELLYFIN_API_KEY or not settings.JELLYFIN_URL:
            return {"total": 0, "albums": []}
        user_id = cls.get_scoped_user_id()
        url = f"{settings.JELLYFIN_URL.rstrip('/')}/Users/{user_id}/Items" if user_id else f"{settings.JELLYFIN_URL.rstrip('/')}/Items"
        params = {
            "includeItemTypes": "MusicAlbum",
            "recursive": "true",
            "sortBy": "SortName",
            "limit": limit,
            "startIndex": offset
        }
        if q and q.strip():
            params["searchTerm"] = q.strip()

        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(url, headers=cls._headers(), params=params)
            if res.status_code == 200:
                data = res.json()
                items = data.get("Items", [])
                total = data.get("TotalRecordCount", len(items))
                albums = []
                for it in items:
                    albums.append({
                        "id": it.get("Id"),
                        "name": it.get("Name", "Unknown album"),
                        "artist": it.get("AlbumArtist") or (it.get("Artists", [""])[0] if it.get("Artists") else "Unknown artist"),
                        "year": it.get("ProductionYear"),
                        "cover_url": f"/api/jellyfin/image/{it.get('Id')}"
                    })
                return {"total": total, "albums": albums}
        return {"total": 0, "albums": []}

    @classmethod
    async def get_album_tracks(cls, album_id: str) -> dict:
        if not settings.JELLYFIN_API_KEY or not settings.JELLYFIN_URL:
            return {"album": None, "tracks": []}
        user_id = cls.get_scoped_user_id()
        url = f"{settings.JELLYFIN_URL.rstrip('/')}/Users/{user_id}/Items" if user_id else f"{settings.JELLYFIN_URL.rstrip('/')}/Items"
        params = {
            "parentId": album_id,
            "fields": "HasLyrics,Path,Artists,Album,RunTimeTicks,IndexNumber,AlbumId,ArtistItems,AlbumArtists,ParentId",
            "sortBy": "IndexNumber"
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(url, headers=cls._headers(), params=params)
            album_info_res = await client.get(f"{url}/{album_id}", headers=cls._headers())
            album_info = {}
            if album_info_res.status_code == 200:
                it = album_info_res.json()
                album_artists = it.get("AlbumArtists") or it.get("ArtistItems") or []
                album_info = {
                    "id": it.get("Id"),
                    "name": it.get("Name"),
                    "artist": it.get("AlbumArtist") or (it.get("Artists", [""])[0] if it.get("Artists") else ""),
                    "artist_id": album_artists[0].get("Id") if album_artists else None,
                    "year": it.get("ProductionYear"),
                    "cover_url": f"/api/jellyfin/image/{it.get('Id')}"
                }
            if res.status_code == 200:
                items = res.json().get("Items", [])
                tracks = [cls._format_track_item(it) for it in items]
                return {"album": album_info, "tracks": tracks}
        return {"album": None, "tracks": []}

    _credited_artists_cache: dict = {}
    CREDITED_ARTISTS_TTL = 300

    @classmethod
    async def _credited_artist_ids(cls, client: httpx.AsyncClient, user_id: Optional[str]) -> Optional[dict]:
        """Artists credited on at least one track as {casefolded name: track artist ID} (cached briefly per user).

        Jellyfin keeps an artist entry after retagging removes its last track (for example a former
        "Main feat. Guest" credit), so the artist list is filtered against the tracks themselves.
        """
        key = user_id or ""
        cached = cls._credited_artists_cache.get(key)
        if cached and time.monotonic() - cached[0] < cls.CREDITED_ARTISTS_TTL:
            return cached[1]
        url = f"{settings.JELLYFIN_URL.rstrip('/')}/Users/{user_id}/Items" if user_id else f"{settings.JELLYFIN_URL.rstrip('/')}/Items"
        res = await client.get(url, headers=cls._headers(), params={
            "includeItemTypes": "Audio", "recursive": "true", "fields": "ArtistItems,AlbumArtists",
            "enableImages": "false", "enableUserData": "false",
        })
        if res.status_code != 200:
            return None
        ids = {}
        for item in res.json().get("Items", []):
            for artist in (item.get("ArtistItems") or []) + (item.get("AlbumArtists") or []):
                if artist.get("Id") and artist.get("Name"):
                    ids.setdefault(artist["Name"].casefold(), artist["Id"])
        cls._credited_artists_cache[key] = (time.monotonic(), ids)
        return ids

    @classmethod
    async def get_artists(cls, q: Optional[str] = None, limit: int = 150, offset: int = 0, user_id: Optional[str] = None) -> dict:
        if not settings.JELLYFIN_API_KEY or not settings.JELLYFIN_URL:
            return {"total": 0, "artists": []}
        user_id = user_id if user_id is not None else cls.get_scoped_user_id()
        url = f"{settings.JELLYFIN_URL.rstrip('/')}/Artists"
        params = {"userId": user_id, "sortBy": "SortName", "enableImages": "false"}
        if q and q.strip():
            params["searchTerm"] = q.strip()

        async with httpx.AsyncClient(timeout=20.0) as client:
            res = await client.get(url, headers=cls._headers(), params=params)
            if res.status_code != 200:
                return {"total": 0, "artists": []}
            items = res.json().get("Items", [])
            credited = await cls._credited_artist_ids(client, user_id)
            if credited is not None:
                # Jellyfin can keep a stale artist entry with the same name after files move; use the
                # ID the tracks reference so the artist page lists them, and show each name once.
                kept, seen = [], set()
                for it in items:
                    name = (it.get("Name") or "").casefold()
                    if name in credited and name not in seen:
                        seen.add(name)
                        kept.append({**it, "Id": credited[name]})
                items = kept
            page = items[offset:offset + limit]
            artists = [{
                "id": it.get("Id"),
                "name": it.get("Name", "Unknown artist"),
                "cover_url": f"/api/jellyfin/image/{it.get('Id')}"
            } for it in page]
            return {"total": len(items), "artists": artists}

    @classmethod
    async def get_artist_tracks(cls, artist_id: str) -> dict:
        if not settings.JELLYFIN_API_KEY or not settings.JELLYFIN_URL:
            return {"artist": None, "tracks": []}
        user_id = cls.get_scoped_user_id()
        url = f"{settings.JELLYFIN_URL.rstrip('/')}/Users/{user_id}/Items" if user_id else f"{settings.JELLYFIN_URL.rstrip('/')}/Items"
        params = {
            "artistIds": artist_id,
            "includeItemTypes": "Audio",
            "recursive": "true",
            "fields": "HasLyrics,Path,Artists,Album,RunTimeTicks,IndexNumber,AlbumId,ArtistItems,AlbumArtists,ParentId",
            "sortBy": "SortName"
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(url, headers=cls._headers(), params=params)
            if res.status_code == 200:
                items = res.json().get("Items", [])
                tracks = [cls._format_track_item(it) for it in items]
                # Name the artist from the matching credit, not the track's full "A feat. B" line.
                artist_name = next((artist.get("Name") for it in items for artist in it.get("ArtistItems") or []
                                    if artist.get("Id") == artist_id and artist.get("Name")), None)
                artist_name = artist_name or (tracks[0]["artist"] if tracks else "Artist")
                artist_info = {
                    "id": artist_id,
                    "name": artist_name,
                    "cover_url": f"/api/jellyfin/image/{artist_id}"
                }
                return {"artist": artist_info, "tracks": tracks}
        return {"artist": None, "tracks": []}

    @classmethod
    async def get_playlist_tracks(cls, playlist_id: str, user_id: Optional[str] = None) -> dict:
        if not settings.JELLYFIN_API_KEY or not settings.JELLYFIN_URL:
            return {"playlist": None, "tracks": []}
        user_id = user_id if user_id is not None else cls.get_scoped_user_id()
        url = f"{settings.JELLYFIN_URL.rstrip('/')}/Playlists/{playlist_id}/Items"
        params = {
            "userId": user_id,
            "fields": "HasLyrics,UserData,PlaylistItemId,Path,Artists,Album,RunTimeTicks,IndexNumber,AlbumId,ArtistItems,AlbumArtists,ParentId"
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(url, headers=cls._headers(), params=params)
            pl_info_res = await client.get(f"{settings.JELLYFIN_URL.rstrip('/')}/Users/{user_id}/Items/{playlist_id}", headers=cls._headers())
            pl_info = {}
            if pl_info_res.status_code == 200:
                it = pl_info_res.json()
                pl_info = {
                    "id": it.get("Id"),
                    "name": it.get("Name"),
                    "track_count": it.get("ChildCount", 0),
                    "cover_url": f"/api/jellyfin/image/{it.get('Id')}"
                }
            if res.status_code == 200:
                items = res.json().get("Items", [])
                tracks = [cls._format_track_item(it) for it in items]
                return {"playlist": pl_info, "tracks": tracks}
        return {"playlist": None, "tracks": []}

    @classmethod
    async def get_track_lyrics(cls, item_id: str) -> Optional[dict]:
        """Recupera la lyrics di una traccia Jellyfin da /Audio/{id}/Lyrics o LRCLIB"""
        if not settings.JELLYFIN_API_KEY or not settings.JELLYFIN_URL:
            return None

        # 1. Prova endpoint Jellyfin nativo /Audio/{id}/Lyrics
        url = f"{settings.JELLYFIN_URL.rstrip('/')}/Audio/{item_id}/Lyrics"
        async with httpx.AsyncClient(timeout=6.0) as client:
            try:
                res = await client.get(url, headers=cls._headers())
                if res.status_code == 200:
                    data = res.json()
                    lyrics_list = data.get("Lyrics", [])
                    if lyrics_list:
                        has_timestamps = any("Start" in l for l in lyrics_list)
                        if has_timestamps:
                            lrc_lines = []
                            for l in lyrics_list:
                                start_ticks = l.get("Start", 0)
                                sec = start_ticks / 10000000
                                mm = int(sec // 60)
                                ss = sec % 60
                                text = l.get("Text", "")
                                lrc_lines.append(f"[{mm:02d}:{ss:05.2f}]{text}")
                            return {"found": True, "synced": "\n".join(lrc_lines), "plain": "\n".join(l.get("Text", "") for l in lyrics_list)}
                        else:
                            plain = "\n".join(l.get("Text", "") for l in lyrics_list)
                            return {"found": True, "synced": None, "plain": plain}
            except Exception as e:
                print(f"[JellyfinService] Errore get_track_lyrics: {e}")

        return None

    @classmethod
    async def save_lrc_for_jellyfin_track(
        cls,
        item_id: str,
        title: str,
        artist: str,
        album: Optional[str] = None
    ) -> dict:
        import os
        from app.services.lyrics_service import LyricsService

        # 1. Recupera metadati traccia da Jellyfin (per avere il path reale e durata)
        user_id = cls.get_scoped_user_id()
        item_path = None
        duration_sec = 0

        async with httpx.AsyncClient(timeout=8.0) as client:
            url = f"{settings.JELLYFIN_URL.rstrip('/')}/Users/{user_id}/Items/{item_id}" if user_id else f"{settings.JELLYFIN_URL.rstrip('/')}/Items/{item_id}"
            try:
                res = await client.get(url, headers=cls._headers())
                if res.status_code == 200:
                    data = res.json()
                    item_path = data.get("Path")
                    duration_sec = (data.get("RunTimeTicks", 0) or 0) // 10000000
            except Exception as e:
                print(f"[JellyfinService] Errore lettura item {item_id}: {e}")

        # 2. Scarica lyrics sincronizzata da LRCLIB
        lyrics = await LyricsService.fetch_lyrics(title, artist, album, duration_sec)
        if not lyrics or (not lyrics.get("synced") and not lyrics.get("plain")):
            return {
                "success": False,
                "message": f"No lyrics found online for '{title} - {artist}'."
            }

        synced_text = lyrics.get("synced")
        plain_text = lyrics.get("plain")
        lrc_content = synced_text if synced_text else plain_text

        # 3. Salva nativamente su Jellyfin tramite API POST /Audio/{itemId}/Lyrics?fileName=...
        jellyfin_uploaded = False
        safe_name = f"{artist} - {title}".replace("/", "_").replace("\\", "_")
        lrc_filename = f"{safe_name}.lrc"
        if item_path:
            lrc_filename = os.path.splitext(os.path.basename(item_path))[0] + ".lrc"

        if settings.JELLYFIN_URL and settings.JELLYFIN_API_KEY:
            try:
                upload_url = f"{settings.JELLYFIN_URL.rstrip('/')}/Audio/{item_id}/Lyrics"
                upload_headers = cls._headers()
                upload_headers["Content-Type"] = "text/plain; charset=utf-8"
                async with httpx.AsyncClient(timeout=10.0) as client:
                    upload_res = await client.post(
                        upload_url,
                        headers=upload_headers,
                        params={"fileName": lrc_filename},
                        content=lrc_content.encode("utf-8")
                    )
                    if upload_res.status_code in (200, 204):
                        jellyfin_uploaded = True
                        print(f"[JellyfinService] Testo salvato direttamente su Jellyfin per traccia {item_id}")
            except Exception as e:
                print(f"[JellyfinService] Errore upload API lyrics su Jellyfin: {e}")

        # 4. Risoluzione percorso di salvataggio su disco (se cartella accessibile/montata)
        saved_on_disk = False
        target_file_path = None

        if item_path:
            direct_dir = os.path.dirname(item_path)
            if os.path.exists(direct_dir):
                target_file_path = os.path.join(direct_dir, lrc_filename)
            else:
                parts = [p for p in item_path.strip("/").split("/") if p]
                for i in range(len(parts) - 1, 0, -1):
                    sub_path = os.path.join(*parts[i-1:-1]) if i > 1 else parts[-2]
                    candidate = os.path.join(settings.MUSIC_OUTPUT_DIR, sub_path)
                    if os.path.exists(candidate):
                        target_file_path = os.path.join(candidate, lrc_filename)
                        break

            if target_file_path:
                try:
                    with open(target_file_path, "w", encoding="utf-8") as f:
                        f.write(lrc_content)
                    saved_on_disk = True
                    # Refreshing the album folder makes Jellyfin pick up the new sidecar right away.
                    await cls.scan_file(target_file_path, wait=0)
                except Exception as e:
                    print(f"[JellyfinService] Errore scrittura LRC {target_file_path}: {e}")

        if jellyfin_uploaded or saved_on_disk:
            msg = f"Lyrics salvata con successo in Jellyfin{' e su disco' if saved_on_disk else ''}!"
            return {
                "success": True,
                "saved_to_disk": saved_on_disk,
                "jellyfin_uploaded": jellyfin_uploaded,
                "target_path": target_file_path or item_path,
                "synced": synced_text,
                "plain": plain_text,
                "message": msg
            }
        else:
            return {
                "success": False,
                "message": f"Could not save lyrics to Jellyfin or disk for '{title}'."
            }
