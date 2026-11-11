import httpx
from contextvars import ContextVar, Token
from typing import List, Optional
from app.config import settings
from app.models.schemas import JellyfinPlaylist

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
    async def authenticate_user(cls, username: str, password: str, jellyfin_url: Optional[str] = None) -> dict:
        """Autentica l'utente con username e password Jellyfin tramite /Users/AuthenticateByName"""
        url = (jellyfin_url or settings.JELLYFIN_URL).rstrip('/')
        if not url:
            return {"success": False, "message": "Jellyfin URL is not configured."}

        auth_endpoint = f"{url}/Users/AuthenticateByName"
        authorization = 'MediaBrowser Client="MusicSeerr", Device="Web", DeviceId="MusicSeerr-Web", Version="1.0.0"'
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
    async def find_track_id(cls, title: str, artist: str, user_id: Optional[str] = None) -> Optional[str]:
        """Cerca l'ID Jellyfin del brano appena scansionato"""
        if not settings.JELLYFIN_API_KEY or not settings.JELLYFIN_URL:
            return None

        scoped_user_id = user_id if user_id is not None else cls.get_scoped_user_id()
        url = f"{settings.JELLYFIN_URL.rstrip('/')}/Users/{scoped_user_id}/Items" if scoped_user_id else f"{settings.JELLYFIN_URL.rstrip('/')}/Items"
        params = {
            "searchTerm": title,
            "includeItemTypes": "Audio",
            "recursive": "true",
            "limit": 5
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                res = await client.get(url, params=params, headers=cls._headers())
                if res.status_code == 200:
                    items = res.json().get("Items", [])
                    for item in items:
                        # Controllo flessibile su artista / titolo
                        item_artists = [a.lower() for a in item.get("Artists", [])]
                        if not artist or any(artist.lower() in a for a in item_artists) or artist.lower() in item.get("Name", "").lower():
                            return item.get("Id")
                    if items:
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
    def _format_track_item(item: dict) -> dict:
        item_id = item.get("Id")
        ticks = item.get("RunTimeTicks", 0) or 0
        total_sec = ticks // 10000000
        mins = total_sec // 60
        secs = total_sec % 60
        duration_formatted = f"{mins}:{secs:02d}"

        artists = item.get("Artists", [])
        artist_name = ", ".join(artists) if artists else item.get("AlbumArtist", "Unknown artist")

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
                album_info = {
                    "id": it.get("Id"),
                    "name": it.get("Name"),
                    "artist": it.get("AlbumArtist") or (it.get("Artists", [""])[0] if it.get("Artists") else ""),
                    "year": it.get("ProductionYear"),
                    "cover_url": f"/api/jellyfin/image/{it.get('Id')}"
                }
            if res.status_code == 200:
                items = res.json().get("Items", [])
                tracks = [cls._format_track_item(it) for it in items]
                return {"album": album_info, "tracks": tracks}
        return {"album": None, "tracks": []}

    @classmethod
    async def get_artists(cls, q: Optional[str] = None, limit: int = 150, offset: int = 0, user_id: Optional[str] = None) -> dict:
        if not settings.JELLYFIN_API_KEY or not settings.JELLYFIN_URL:
            return {"total": 0, "artists": []}
        user_id = user_id if user_id is not None else cls.get_scoped_user_id()
        url = f"{settings.JELLYFIN_URL.rstrip('/')}/Artists"
        params = {
            "userId": user_id,
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
                artists = []
                for it in items:
                    artists.append({
                        "id": it.get("Id"),
                        "name": it.get("Name", "Unknown artist"),
                        "cover_url": f"/api/jellyfin/image/{it.get('Id')}"
                    })
                return {"total": total, "artists": artists}
        return {"total": 0, "artists": []}

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
                artist_name = tracks[0]["artist"] if tracks else "Artist"
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
                    await cls.refresh_library()
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
