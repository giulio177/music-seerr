import os
import re
import random
import asyncio
import time
from fastapi import FastAPI, Query, HTTPException, BackgroundTasks, Request, UploadFile, File, Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from typing import List, Optional, Dict, Any

from app.config import settings, save_webapp_config
from app.models.schemas import (
    SearchResponse,
    TrackItem,
    DownloadRequest,
    DownloadJob,
    JellyfinPlaylist
)
from app.services.search_service import SearchService
from app.services.download_service import download_manager
from app.services.jellyfin_service import JellyfinService
from app.services.playlist_csv_service import PlaylistCSVService
from app.services.user_data_service import UserDataService

app = FastAPI(
    title="Music Seerr API",
    description="Backend per la ricerca musicale, preview, download e sync Jellyfin",
    version="1.0.0"
)

# Cartella statica per la Web UI
static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

APP_SESSION_COOKIE = "music_seerr_session"
ADMIN_ROLE_CHECK_CACHE: dict[str, tuple[float, bool]] = {}

@app.middleware("http")
async def resolve_app_session(request: Request, call_next):
    principal = UserDataService.get_session(request.cookies.get(APP_SESSION_COOKIE))
    if principal and principal.get("is_admin"):
        principal = dict(principal)
        cached = ADMIN_ROLE_CHECK_CACHE.get(principal.get("id"))
        now = time.monotonic()
        if cached and now - cached[0] < 60:
            principal["is_admin"] = cached[1]
        else:
            users = await JellyfinService.get_users()
            active_user = next((user for user in users if user.get("id") == principal.get("id")), None)
            is_still_admin = bool(active_user and active_user.get("is_admin"))
            ADMIN_ROLE_CHECK_CACHE[principal.get("id")] = (now, is_still_admin)
            principal["is_admin"] = is_still_admin
            if len(ADMIN_ROLE_CHECK_CACHE) > 256:
                ADMIN_ROLE_CHECK_CACHE.clear()
    request.state.app_user = principal
    scope_token = JellyfinService.set_request_user_id(principal.get("id") if principal and not principal.get("is_guest") else None)
    try:
        return await call_next(request)
    finally:
        JellyfinService.reset_request_user_id(scope_token)

def require_app_user(request: Request) -> dict:
    principal = getattr(request.state, "app_user", None)
    if not principal:
        raise HTTPException(status_code=401, detail="Sign in with Jellyfin or continue as a guest.")
    return principal

def require_admin(request: Request) -> dict:
    principal = require_app_user(request)
    if not principal.get("is_admin"):
        raise HTTPException(status_code=403, detail="This action is reserved for administrators.")
    return principal

def require_jellyfin_user(request: Request) -> dict:
    principal = require_app_user(request)
    if principal.get("is_guest") or not principal.get("id"):
        raise HTTPException(status_code=403, detail="Sign in with a Jellyfin account to access this library.")
    return principal

async def resolve_playlist_access(playlist_id: str, principal: dict, for_mutation: bool = False, owner_user_id: Optional[str] = None):
    if principal.get("is_guest"):
        raise HTTPException(status_code=403, detail="Sign in with Jellyfin to use playlists.")
    if owner_user_id and not principal.get("is_admin") and owner_user_id != principal.get("id"):
        raise HTTPException(status_code=403, detail="You can only select your own Jellyfin account.")
    if principal.get("is_admin"):
        users = await JellyfinService.get_users()
        user_names = {user.get("id"): user.get("name") for user in users if user.get("id")}
        accessible_match = None
        known_user_ids = {user.get("id") for user in users if user.get("id")}
        if owner_user_id:
            if owner_user_id not in known_user_ids:
                raise HTTPException(status_code=400, detail="The selected Jellyfin account no longer exists.")
            selected_user = next(user for user in users if user.get("id") == owner_user_id)
            for playlist in await JellyfinService.get_playlists(user_id=owner_user_id):
                if playlist.id != playlist_id:
                    continue
                if playlist.owner_user_id and playlist.owner_user_id != owner_user_id:
                    raise HTTPException(status_code=403, detail="Jellyfin reports that this playlist belongs to another account.")
                playlist.owner_user_id = owner_user_id
                playlist.owner_name = user_names.get(owner_user_id) or selected_user.get("name")
                return playlist, owner_user_id
            raise HTTPException(status_code=404, detail="The selected Jellyfin account cannot access this playlist.")
        # Jellyfin may omit OwnerUserId even for playlists returned in the
        # authenticated administrator's own account scope. In that case the
        # verified session identifies the owner without a client-supplied ID.
        for playlist in await JellyfinService.get_playlists(user_id=principal.get("id")):
            if playlist.id != playlist_id:
                continue
            if playlist.owner_user_id and playlist.owner_user_id != principal.get("id"):
                break
            playlist.owner_user_id = principal.get("id")
            playlist.owner_name = user_names.get(principal.get("id")) or principal.get("name")
            return playlist, principal.get("id")
        for user in users:
            access_user_id = user.get("id")
            if not access_user_id:
                continue
            for playlist in await JellyfinService.get_playlists(user_id=access_user_id):
                if playlist.id == playlist_id:
                    if playlist.owner_user_id:
                        playlist.owner_name = user_names.get(playlist.owner_user_id, playlist.owner_name)
                        if playlist.owner_user_id in known_user_ids:
                            return playlist, playlist.owner_user_id
                    if accessible_match is None:
                        accessible_match = (playlist, access_user_id)
        if accessible_match:
            if for_mutation:
                raise HTTPException(status_code=409, detail="Jellyfin did not report the playlist owner. Select the owner account in the CSV panel.")
            return accessible_match
    else:
        if owner_user_id and owner_user_id != principal.get("id"):
            raise HTTPException(status_code=403, detail="You can only select your own Jellyfin account.")
        for playlist in await JellyfinService.get_playlists(user_id=principal.get("id")):
            if playlist.id == playlist_id:
                if for_mutation:
                    if not playlist.owner_user_id:
                        raise HTTPException(status_code=409, detail="Jellyfin did not report the playlist owner. Ask an administrator to verify it.")
                    if playlist.owner_user_id != principal.get("id"):
                        raise HTTPException(status_code=403, detail="You can only edit playlists owned by your Jellyfin account.")
                if playlist.owner_user_id == principal.get("id"):
                    playlist.owner_name = principal.get("name")
                return playlist, principal.get("id")
    raise HTTPException(status_code=404, detail="Playlist not found or inaccessible.")

def set_session_cookie(response: Response, token: str, request: Request):
    response.set_cookie(
        APP_SESSION_COOKIE,
        token,
        max_age=UserDataService.SESSION_TTL,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
    )

def _user_payload(user: dict) -> dict:
    return {
        "id": user.get("id"),
        "name": user.get("name"),
        "is_admin": bool(user.get("is_admin")),
        "is_guest": bool(user.get("is_guest")),
        "image_url": f"/api/jellyfin/user-image/{user.get('id')}" if user.get("id") and not user.get("is_guest") else "",
    }

@app.get("/", include_in_schema=False)
async def serve_index():
    index_file = os.path.join(static_dir, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return {"message": "Music Seerr API is running! Access /docs for API documentation."}

@app.get("/api/health")
async def health_check():
    music_ready = bool(settings.MUSIC_OUTPUT_DIR and os.path.isdir(settings.MUSIC_OUTPUT_DIR) and os.access(settings.MUSIC_OUTPUT_DIR, os.W_OK))
    if not music_ready:
        raise HTTPException(status_code=503, detail="The music folder is not writable.")
    return {"status": "healthy", "jellyfin_configured": bool(settings.JELLYFIN_API_KEY)}

def _normalize_str(text: Optional[str]) -> str:
    if not text:
        return ""
    t = text.lower()
    t = re.sub(r'[\(\[\{].*?[\)\]\}]', '', t)
    t = re.sub(r'[^a-z0-9]', '', t)
    return t

def _is_match(t_title: str, t_artist: str, j_title: str, j_artist: str) -> bool:
    norm_tt = _normalize_str(t_title)
    norm_jt = _normalize_str(j_title)
    if not norm_tt or not norm_jt:
        return False
    title_matches = (norm_tt == norm_jt) or (len(norm_tt) > 5 and norm_tt in norm_jt) or (len(norm_jt) > 5 and norm_jt in norm_tt)
    if not title_matches:
        return False
    norm_ta = _normalize_str(t_artist)
    norm_ja = _normalize_str(j_artist)
    if not norm_ta or not norm_ja:
        return True
    return (norm_ta in norm_ja) or (norm_ja in norm_ta)

@app.get("/api/search", response_model=SearchResponse)
async def search_music(request: Request, q: str = Query(..., min_length=1, description="Titolo o artista da cercare"), limit: int = 25):
    """
    Cerca un brano con precisione stile Spotify/Apple Music.
    Restituisce preview_url a 30s in MP3, copertine HD e indica se il brano è già presente in Jellyfin.
    """
    require_app_user(request)
    search_task = SearchService.search(q, limit=limit)
    principal = require_app_user(request)
    jelly_task = (JellyfinService.get_library_tracks(q=q, limit=100)
                  if not principal.get("is_guest") else asyncio.sleep(0, result={"tracks": []}))
    tracks, jelly_data = await asyncio.gather(search_task, jelly_task, return_exceptions=True)

    if isinstance(tracks, Exception):
        tracks = []
    
    if isinstance(tracks, list) and not isinstance(jelly_data, Exception) and isinstance(jelly_data, dict):
        jelly_tracks = jelly_data.get("tracks", [])
        for t in tracks:
            for jt in jelly_tracks:
                if _is_match(t.title, t.artist, jt.get("title", ""), jt.get("artist", "")):
                    t.in_jellyfin = True
                    t.jellyfin_id = jt.get("id")
                    break

    return SearchResponse(
        query=q,
        total=len(tracks),
        results=tracks
    )

@app.get("/api/recommendations")
async def get_recommendations(request: Request, limit: int = 12, user_id: Optional[str] = None):
    """
    Restituisce brani consigliati basati sugli artisti presenti nella libreria Jellyfin dell'utente.
    """
    principal = require_app_user(request)
    target_user_id = principal.get("id", "guest")
    target_name = principal.get("name", "Guest")
    if user_id:
        if not principal.get("is_admin"):
            raise HTTPException(status_code=403, detail="Only administrators can view recommendations for other users.")
        known_users = await JellyfinService.get_users()
        target = next((item for item in known_users if item.get("id") == user_id), None)
        if not target:
            raise HTTPException(status_code=404, detail="Jellyfin user not found.")
        target_user_id = user_id
        target_name = target.get("name", target_user_id)
    is_guest_target = target_user_id.startswith("guest:")
    artists_res = {"artists": []} if is_guest_target else await JellyfinService.get_artists(limit=100, user_id=target_user_id)
    artists = artists_res.get("artists", []) if isinstance(artists_res, dict) else []

    recent_plays = UserDataService.list_plays(user_id=target_user_id, limit=30)
    recent_artists = list(dict.fromkeys(play["artist"] for play in recent_plays if play.get("artist")))
    artist_pool = list(dict.fromkeys(recent_artists + [a.get("name") for a in artists if a.get("name")]))
    if not artist_pool:
        tracks = await SearchService.search("Pop Top Hits", limit=limit)
        return {"based_on_artists": [], "tracks": tracks, "for_user": target_name}

    # Stable per-user daily sampling keeps the feed scoped and avoids one shared random feed.
    seed = f"{target_user_id}:{time.strftime('%Y-%m-%d')}"
    rng = random.Random(seed)
    sample_size = min(4, len(artist_pool))
    artist_names = rng.sample(artist_pool, sample_size)
    
    all_recs = []
    for name in artist_names:
        try:
            recs = await SearchService.search(name, limit=3)
            all_recs.extend(recs)
        except Exception:
            continue
            
    seen_ids = set()
    unique_recs = []
    for r in all_recs:
        if r.id not in seen_ids:
            seen_ids.add(r.id)
            unique_recs.append(r)
            
    # Controlla per ognuno se è già presente in Jellyfin
    for t in unique_recs[:limit]:
        try:
            jt_match = await JellyfinService.find_track_id(t.title, t.artist, user_id=None if is_guest_target else target_user_id)
            if jt_match:
                t.in_jellyfin = True
                t.jellyfin_id = jt_match
        except Exception:
            pass
            
    return {
        "based_on_artists": artist_names,
        "tracks": unique_recs[:limit],
        "for_user": target_name
    }

@app.get("/api/jellyfin/playlists", response_model=List[JellyfinPlaylist])
async def list_jellyfin_playlists(request: Request):
    """Restituisce l'elenco delle playlist da Jellyfin per il menu a tendina"""
    principal = require_jellyfin_user(request)
    playlists = await JellyfinService.get_playlists(user_id=principal.get("id"))
    for playlist in playlists:
        if playlist.owner_user_id == principal.get("id"):
            playlist.owner_name = principal.get("name")
    return playlists

@app.get("/api/admin/users")
async def list_users_for_admin(request: Request):
    require_admin(request)
    return {"users": await JellyfinService.get_users()}

@app.get("/api/admin/jellyfin/playlists")
async def list_all_jellyfin_playlists(request: Request):
    require_admin(request)
    users = await JellyfinService.get_users()
    user_names = {user.get("id"): user.get("name", "Jellyfin") for user in users if user.get("id")}
    playlists = []
    seen_playlist_ids = set()
    for user in users:
        user_id = user.get("id")
        if not user_id:
            continue
        for playlist in await JellyfinService.get_playlists(user_id=user_id):
            if playlist.id in seen_playlist_ids:
                continue
            seen_playlist_ids.add(playlist.id)
            playlist.owner_name = user_names.get(playlist.owner_user_id) if playlist.owner_user_id else None
            playlists.append(playlist)
    return playlists

@app.post("/api/jellyfin/refresh")
async def refresh_jellyfin_library(request: Request):
    """Forza una scansione manuale della libreria Jellyfin"""
    require_admin(request)
    success = await JellyfinService.refresh_library()
    return {"success": success}

@app.get("/api/jellyfin/library/tracks")
async def get_jellyfin_library_tracks(
    request: Request,
    q: Optional[str] = None,
    playlist_id: Optional[str] = None,
    has_lyrics: Optional[bool] = None,
    limit: int = 50,
    offset: int = 0
):
    """Cerca ed elenca i brani dalla libreria Jellyfin stile Dropped Needle"""
    require_jellyfin_user(request)
    return await JellyfinService.get_library_tracks(
        q=q,
        playlist_id=playlist_id,
        has_lyrics=has_lyrics,
        limit=limit,
        offset=offset
    )

@app.get("/api/jellyfin/albums")
async def get_jellyfin_albums(request: Request, q: Optional[str] = None, limit: int = 150, offset: int = 0):
    """Elenco album da Jellyfin"""
    require_jellyfin_user(request)
    return await JellyfinService.get_albums(q=q, limit=limit, offset=offset)

@app.get("/api/jellyfin/album/{album_id}/tracks")
async def get_jellyfin_album_tracks(album_id: str, request: Request):
    """Brani di uno specifico album Jellyfin"""
    require_jellyfin_user(request)
    return await JellyfinService.get_album_tracks(album_id=album_id)

@app.get("/api/jellyfin/artists")
async def get_jellyfin_artists(request: Request, q: Optional[str] = None, limit: int = 150, offset: int = 0):
    """Elenco artisti da Jellyfin"""
    require_jellyfin_user(request)
    return await JellyfinService.get_artists(q=q, limit=limit, offset=offset)

@app.get("/api/jellyfin/artist/{artist_id}/tracks")
async def get_jellyfin_artist_tracks(artist_id: str, request: Request):
    """Tutti i brani di uno specifico artista Jellyfin"""
    require_jellyfin_user(request)
    return await JellyfinService.get_artist_tracks(artist_id=artist_id)

@app.get("/api/jellyfin/playlist/{playlist_id}/tracks")
async def get_jellyfin_playlist_tracks(playlist_id: str, request: Request):
    """Tutti i brani di una specifica playlist Jellyfin"""
    principal = require_app_user(request)
    playlist, access_user_id = await resolve_playlist_access(playlist_id, principal)
    return await JellyfinService.get_playlist_tracks(playlist_id=playlist.id, user_id=access_user_id)

@app.api_route("/api/jellyfin/stream/{item_id}", methods=["GET", "HEAD"])
async def stream_jellyfin_track(item_id: str, request: Request, format: Optional[str] = None):
    """Proxy stream audio da Jellyfin con adattamento automatico e supporto Same-Origin per Web Audio Analyser"""
    from fastapi.responses import StreamingResponse, RedirectResponse
    from starlette.background import BackgroundTask
    import httpx

    require_jellyfin_user(request)
    if not settings.JELLYFIN_URL or not settings.JELLYFIN_API_KEY:
        raise HTTPException(status_code=400, detail="Jellyfin is not configured.")
    force_mp3 = (format == "mp3")
    stream_url = await JellyfinService.get_stream_url(item_id, force_mp3=force_mp3)
    if not stream_url:
        stream_url = f"{settings.JELLYFIN_URL.rstrip('/')}/Audio/{item_id}/stream?static=true&api_key={settings.JELLYFIN_API_KEY}"

    forward_headers = {}
    if "range" in request.headers:
        forward_headers["range"] = request.headers["range"]

    client = httpx.AsyncClient(timeout=60.0)
    try:
        req = client.build_request("GET", stream_url, headers=forward_headers)
        r = await client.send(req, stream=True)
        resp_headers = {
            "Content-Type": r.headers.get("content-type", "audio/mpeg"),
            "Accept-Ranges": r.headers.get("accept-ranges", "bytes"),
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
            "Access-Control-Allow-Headers": "*"
        }
        if "content-range" in r.headers:
            resp_headers["Content-Range"] = r.headers["content-range"]
        if "content-length" in r.headers:
            resp_headers["Content-Length"] = r.headers["content-length"]

        return StreamingResponse(
            r.aiter_raw(),
            status_code=r.status_code,
            headers=resp_headers,
            background=BackgroundTask(r.aclose)
        )
    except Exception as e:
        await client.aclose()
        return RedirectResponse(url=stream_url, headers={"Access-Control-Allow-Origin": "*"})

@app.get("/api/jellyfin/image/{item_id}")
async def get_jellyfin_image(item_id: str, request: Request):
    """Proxy Jellyfin artwork so browsers never need to resolve the container hostname."""
    require_jellyfin_user(request)
    if not settings.JELLYFIN_URL or not settings.JELLYFIN_API_KEY:
        raise HTTPException(status_code=400, detail="Jellyfin is not configured.")
    image_url = f"{settings.JELLYFIN_URL.rstrip('/')}/Items/{item_id}/Images/Primary"
    return await _proxy_jellyfin_image(image_url, params={"maxWidth": 300, "maxHeight": 300})

@app.get("/api/jellyfin/user-image/{user_id}")
async def get_jellyfin_user_image(user_id: str, request: Request):
    """Proxy a Jellyfin user artwork using server-side API credentials."""
    principal = require_app_user(request)
    if user_id != principal.get("id") and not principal.get("is_admin"):
        raise HTTPException(status_code=403, detail="You cannot view another user’s profile.")
    if not settings.JELLYFIN_URL or not settings.JELLYFIN_API_KEY:
        raise HTTPException(status_code=400, detail="Jellyfin is not configured.")
    image_url = f"{settings.JELLYFIN_URL.rstrip('/')}/Users/{user_id}/Images/Primary"
    return await _proxy_jellyfin_image(image_url)


async def _proxy_jellyfin_image(image_url: str, params: Optional[dict] = None) -> Response:
    """Fetch Jellyfin images inside the backend network and cache them briefly in the browser."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            image = await client.get(image_url, params=params, headers=JellyfinService._headers())
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Could not retrieve the image from Jellyfin.") from exc
    if image.status_code != 200:
        return Response(status_code=image.status_code, headers={"Cache-Control": "private, max-age=60"})
    content_type = image.headers.get("content-type", "image/jpeg")
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=502, detail="Jellyfin returned a non-image response.")
    if len(image.content) > 8 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="The Jellyfin image is too large.")
    return Response(
        content=image.content,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
    )

# === SETUP & AUTH ENDPOINTS ===

@app.get("/api/setup/status")
async def get_setup_status(request: Request):
    """Restituisce lo stato del set-up dell'applicazione"""
    jellyfin_configured = bool(settings.JELLYFIN_URL and settings.JELLYFIN_API_KEY)
    music_dir_exists = os.path.exists(settings.MUSIC_OUTPUT_DIR) if settings.MUSIC_OUTPUT_DIR else False
    is_completed = getattr(settings, "SETUP_COMPLETED", False) and jellyfin_configured

    return {
        "setup_completed": is_completed,
        "jellyfin_url": settings.JELLYFIN_URL,
        "jellyfin_configured": jellyfin_configured,
        "jellyfin_api_key": settings.JELLYFIN_API_KEY or "" if getattr(request.state, "app_user", None) and request.state.app_user.get("is_admin") else "",
        "music_dir": settings.MUSIC_OUTPUT_DIR if getattr(request.state, "app_user", None) and request.state.app_user.get("is_admin") else "",
        "music_dir_exists": music_dir_exists,
        "current_user_id": settings.JELLYFIN_USER_ID or "" if getattr(request.state, "app_user", None) and request.state.app_user.get("is_admin") else "",
        "language": getattr(settings, "LANGUAGE", "en") or "en"
    }

@app.post("/api/setup/complete")
async def complete_setup(payload: dict, request: Request):
    """Completa la procedura guidata di set-up e salva la configurazione"""
    principal = getattr(request.state, "app_user", None)
    if getattr(settings, "SETUP_COMPLETED", False) and (not principal or not principal.get("is_admin")):
        raise HTTPException(status_code=403, detail="Only an administrator can change the global configuration.")
    payload["setup_completed"] = True
    save_webapp_config(payload)
    return {
        "success": True,
        "message": "Setup completed successfully!",
        "setup_completed": True,
        "music_dir": settings.MUSIC_OUTPUT_DIR,
        "jellyfin_url": settings.JELLYFIN_URL,
        "jellyfin_user_id": settings.JELLYFIN_USER_ID or ""
    }

@app.get("/api/auth/users")
async def get_auth_users(request: Request, jellyfin_url: Optional[str] = None, api_key: Optional[str] = None):
    """Restituisce l'elenco degli utenti Jellyfin rilevati dal server"""
    if getattr(settings, "SETUP_COMPLETED", False):
        require_admin(request)
    users = await JellyfinService.get_users(jellyfin_url=jellyfin_url, api_key=api_key)
    return {"users": users}

@app.post("/api/auth/login")
async def auth_login(payload: dict, request: Request, response: Response):
    """Autentica l'utente con username e password Jellyfin"""
    username = payload.get("username", "").strip()
    password = payload.get("password", "")
    # Once configured, authenticate only against the server owned by this app.
    # The setup wizard may supply a temporary URL before configuration is saved.
    jellyfin_url = settings.JELLYFIN_URL if getattr(settings, "SETUP_COMPLETED", False) else payload.get("jellyfin_url")

    if not username:
        raise HTTPException(status_code=400, detail="A username is required.")

    res = await JellyfinService.authenticate_user(username=username, password=password, jellyfin_url=jellyfin_url)
    if not res.get("success"):
        raise HTTPException(status_code=401, detail=res.get("message", "Invalid credentials."))

    user = res.get("user", {})
    if not user.get("id"):
        raise HTTPException(status_code=401, detail="Jellyfin did not return a valid identity.")
    UserDataService.delete_session(request.cookies.get(APP_SESSION_COOKIE))
    set_session_cookie(response, UserDataService.create_session(user), request)
    # Do not return Jellyfin's access token to the browser.
    return {"success": True, "user": user}

@app.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response):
    """Termina la sessione utente locale"""
    UserDataService.delete_session(request.cookies.get(APP_SESSION_COOKIE))
    response.delete_cookie(APP_SESSION_COOKIE, path="/", httponly=True, samesite="lax")
    return {"success": True, "message": "Signed out successfully."}

@app.post("/api/auth/guest")
async def auth_guest(request: Request, response: Response):
    UserDataService.delete_session(request.cookies.get(APP_SESSION_COOKIE))
    token = UserDataService.create_session(is_guest=True)
    set_session_cookie(response, token, request)
    return {"success": True, "user": {"name": "Guest", "is_guest": True, "is_admin": False}}

@app.get("/api/auth/session")
async def get_current_session(request: Request):
    principal = getattr(request.state, "app_user", None)
    return {"authenticated": bool(principal), "user": _user_payload(principal) if principal else None}

@app.get("/api/me/preferences")
async def get_user_preferences(request: Request):
    principal = require_app_user(request)
    return {"show_other_users_controls": UserDataService.get_show_other_users_controls(principal)}

@app.post("/api/me/preferences")
async def update_user_preferences(payload: dict, request: Request):
    principal = require_admin(request)
    show_controls = bool(payload.get("show_other_users_controls", True))
    UserDataService.set_show_other_users_controls(principal, show_controls)
    return {"show_other_users_controls": show_controls}

@app.get("/api/history/downloads")
async def get_download_history(request: Request, limit: int = 100):
    principal = require_app_user(request)
    return UserDataService.list_downloads(user_id=principal.get("id"), limit=limit)

@app.get("/api/history/plays")
async def get_play_history(request: Request, limit: int = 100):
    principal = require_app_user(request)
    return UserDataService.list_plays(user_id=principal.get("id"), limit=limit)

@app.post("/api/history/plays")
async def create_play_history(payload: dict, request: Request):
    principal = require_app_user(request)
    source = payload.get("source", "preview")
    if source not in {"preview", "jellyfin", "downloaded"}:
        raise HTTPException(status_code=400, detail="Invalid playback source.")
    saved = UserDataService.record_play(principal, payload.get("track") or {}, source=source)
    return {"recorded": saved}

@app.get("/api/admin/history/downloads")
async def get_all_download_history(request: Request, limit: int = 200, user_id: Optional[str] = None):
    require_admin(request)
    return UserDataService.list_downloads(user_id=user_id, limit=limit)

@app.get("/api/admin/history/plays")
async def get_all_play_history(request: Request, limit: int = 200, user_id: Optional[str] = None):
    require_admin(request)
    return UserDataService.list_plays(user_id=user_id, limit=limit)

@app.get("/api/jellyfin/track-lyrics/{item_id}")
async def get_jellyfin_track_lyrics(item_id: str, request: Request, title: Optional[str] = None, artist: Optional[str] = None):
    """Recupera le lyrics di una traccia (da Jellyfin nativo o da LRCLIB fallback)"""
    require_jellyfin_user(request)
    lyrics_data = await JellyfinService.get_track_lyrics(item_id)
    if lyrics_data:
        return lyrics_data
    if title and artist:
        from app.services.lyrics_service import LyricsService
        lyrics = await LyricsService.fetch_lyrics(title, artist)
        if lyrics and (lyrics.get("synced") or lyrics.get("plain")):
            return {"found": True, "synced": lyrics.get("synced"), "plain": lyrics.get("plain")}
    return {"found": False, "message": "No lyrics found for this track."}

@app.post("/api/jellyfin/save-lyrics")
async def save_jellyfin_lyrics(payload: dict, request: Request):
    """Scarica la lyrics da LRCLIB e la salva sul disco nella cartella del brano Jellyfin"""
    require_jellyfin_user(request)
    item_id = payload.get("item_id")
    title = payload.get("title")
    artist = payload.get("artist")
    album = payload.get("album")
    if not item_id or not title or not artist:
        raise HTTPException(status_code=400, detail="item_id, title, and artist are required.")

    result = await JellyfinService.save_lrc_for_jellyfin_track(
        item_id=item_id,
        title=title,
        artist=artist,
        album=album
    )
    return result

@app.get("/api/jellyfin/download-lrc-file")
async def download_lrc_file(item_id: str, request: Request, title: str, artist: str, album: Optional[str] = None):
    """Scarica il file .lrc via browser se la cartella Jellyfin non è montata"""
    from fastapi.responses import Response
    require_jellyfin_user(request)
    from app.services.lyrics_service import LyricsService
    lyrics = await LyricsService.fetch_lyrics(title, artist, album)
    if not lyrics or not (lyrics.get("synced") or lyrics.get("plain")):
        raise HTTPException(status_code=404, detail="Lyrics not found.")
    content = lyrics.get("synced") or lyrics.get("plain")
    filename = f"{artist} - {title}.lrc".replace("/", "_")
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@app.post("/api/download", response_model=DownloadJob)
async def start_download(req: DownloadRequest, request: Request):
    """
    Invia il comando di download con un click e associa la playlist scelta.
    """
    principal = require_app_user(request)
    playlist_user_id = None
    playlist_name = req.playlist_name
    if req.playlist_id:
        selected_playlist, playlist_user_id = await resolve_playlist_access(
            req.playlist_id, principal, for_mutation=True, owner_user_id=req.owner_user_id
        )
        playlist_name = selected_playlist.name
    job = await download_manager.create_job(
        track=req.track,
        playlist_id=req.playlist_id,
        playlist_name=playlist_name,
        owner=principal,
        playlist_user_id=playlist_user_id
    )
    return job

@app.get("/api/download/jobs", response_model=List[DownloadJob])
async def get_download_jobs(request: Request, all_users: bool = False, user_id: Optional[str] = None):
    """Restituisce i job correnti dell'utente, oppure il totale solo per un admin"""
    principal = require_app_user(request)
    if all_users and not principal.get("is_admin"):
        raise HTTPException(status_code=403, detail="This action is reserved for administrators.")
    if user_id and (not all_users or not principal.get("is_admin")):
        raise HTTPException(status_code=403, detail="The user filter is reserved for administrators.")
    return download_manager.list_jobs(owner_id=principal.get("id"), include_all=all_users, user_id=user_id)

@app.get("/api/download/job/{job_id}", response_model=DownloadJob)
async def get_job_status(job_id: str, request: Request):
    """Controlla lo stato di avanzamento di un download specifico"""
    job = download_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    principal = require_app_user(request)
    if job.owner_user_id != principal.get("id") and not principal.get("is_admin"):
        raise HTTPException(status_code=403, detail="This download belongs to another user.")
    return job

@app.post("/api/download/job/{job_id}/retry", response_model=DownloadJob)
async def retry_download_job(job_id: str, request: Request):
    """Queue a fresh attempt for one of the signed-in user's failed/interrupted jobs."""
    principal = require_app_user(request)
    previous = download_manager.get_job(job_id)
    if not previous:
        raise HTTPException(status_code=404, detail="Job not found or unavailable for retry.")
    if previous.owner_user_id != principal.get("id"):
        raise HTTPException(status_code=403, detail="You can only retry downloads from your account.")
    if previous.status not in {"failed", "interrupted"}:
        raise HTTPException(status_code=409, detail="This download cannot be queued again.")

    playlist_user_id = None
    playlist_name = previous.playlist_name
    if previous.playlist_id:
        playlist, playlist_user_id = await resolve_playlist_access(previous.playlist_id, principal, for_mutation=True)
        playlist_name = playlist.name

    retried_job = await download_manager.create_job(
        track=previous.track,
        playlist_id=previous.playlist_id,
        playlist_name=playlist_name,
        owner=principal,
        playlist_user_id=playlist_user_id,
    )
    previous.status = "retried"
    previous.message = "Nuovo tentativo aggiunto alla coda."
    UserDataService.update_download_status(previous.job_id, "retried")
    return retried_job

# --- NUOVE FUNZIONALITÀ: LIBRERIA SCARICATA & LYRICS ---

@app.get("/api/library/downloaded")
async def get_downloaded_library(request: Request):
    """Restituisce l'elenco di tutti i brani scaricati nella cartella locale"""
    require_jellyfin_user(request)
    tracks = []
    base_dir = settings.MUSIC_OUTPUT_DIR
    if not os.path.exists(base_dir):
        return []

    for root, _, files in os.walk(base_dir):
        for file in files:
            if file.endswith((".mp3", ".flac", ".m4a")):
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, base_dir)
                
                # Nome file e traccia
                base_name, _ = os.path.splitext(file)
                lrc_path = os.path.join(root, f"{base_name}.lrc")
                has_lrc = os.path.exists(lrc_path)

                # Artista e album da cartelle se presenti
                parts = rel_path.split(os.sep)
                artist = parts[0] if len(parts) > 2 else "Local artist"
                album = parts[1] if len(parts) > 2 else "Singoli"
                title = base_name

                size_mb = round(os.path.getsize(full_path) / (1024 * 1024), 2)

                tracks.append({
                    "title": title,
                    "artist": artist,
                    "album": album,
                    "file_name": file,
                    "rel_path": rel_path,
                    "full_path": full_path,
                    "size_mb": size_mb,
                    "has_lrc": has_lrc,
                    "stream_url": f"/api/library/stream/{rel_path}"
                })
    return tracks

@app.get("/api/library/stream/{rel_path:path}")
async def stream_local_track(rel_path: str, request: Request):
    """Permette lo streaming completo dal browser dei brani scaricati"""
    require_jellyfin_user(request)
    base_dir = os.path.realpath(settings.MUSIC_OUTPUT_DIR)
    full_path = os.path.realpath(os.path.join(base_dir, rel_path))
    if os.path.commonpath((base_dir, full_path)) != base_dir:
        raise HTTPException(status_code=403, detail="Path is not allowed.")
    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail="File not found.")
    return FileResponse(full_path, media_type="audio/mpeg", filename=os.path.basename(full_path))

@app.get("/api/lyrics")
async def get_lyrics(title: str, artist: str, request: Request):
    """Cerca e restituisce il testo sincronizzato e plain per un brano"""
    require_app_user(request)
    from app.services.lyrics_service import LyricsService
    data = await LyricsService.fetch_lyrics(title, artist)
    if not data:
        raise HTTPException(status_code=404, detail="Lyrics not found.")
    return data

@app.post("/api/library/get-or-fetch-lyrics")
async def get_or_fetch_lyrics(payload: dict, request: Request):
    """
    1. Cerca il file .lrc nella cartella locale del brano.
    2. Se non c'è, scarica automaticamente da LRCLIB e lo salva sul disco.
    3. Restituisce il testo trovato o un messaggio chiaro di errore per il popup.
    """
    require_jellyfin_user(request)
    from app.services.lyrics_service import LyricsService
    full_path = payload.get("full_path")
    artist = payload.get("artist", "")
    title = payload.get("title", "")

    base_dir = os.path.realpath(settings.MUSIC_OUTPUT_DIR)
    full_path = os.path.realpath(full_path) if full_path else ""
    if not full_path or os.path.commonpath((base_dir, full_path)) != base_dir or not os.path.exists(full_path):
        raise HTTPException(status_code=400, detail="Audio file not found on disk.")

    result = await LyricsService.get_or_fetch(full_path, artist, title)
    if not result:
        return {
            "found": False,
            "message": f"No lyrics found for '{artist} - {title}' on disk or LRCLIB."
        }

    return {
        "found": True,
        "source": result.get("source"),  # "disk" o "downloaded"
        "synced": result.get("synced"),
        "plain": result.get("plain"),
        "lrc_path": result.get("lrc_path"),
        "message": "Lyrics loaded from disk" if result.get("source") == "disk" else "Lyrics downloaded and saved alongside the track."
    }

@app.get("/api/config/status")
async def get_config_status(request: Request):
    """Verifica lo stato della cartella musicale e della mappatura Docker"""
    require_admin(request)
    dir_path = settings.MUSIC_OUTPUT_DIR
    exists = os.path.exists(dir_path)
    return {
        "music_dir": dir_path,
        "is_mapped": exists,
        "jellyfin_configured": bool(settings.JELLYFIN_API_KEY)
    }

# --- GESTIONE IMPOSTAZIONI ---

@app.get("/api/settings")
async def get_settings(request: Request):
    """Restituisce le impostazioni attuali di configurazione"""
    principal = getattr(request.state, "app_user", None)
    if principal and not principal.get("is_admin"):
        return {"language": getattr(settings, "LANGUAGE", "en") or "en", "setup_completed": getattr(settings, "SETUP_COMPLETED", False)}
    if getattr(settings, "SETUP_COMPLETED", False) and not principal:
        raise HTTPException(status_code=401, detail="Sign in as an administrator to manage configuration.")
    jellyfin_paths = await JellyfinService.get_music_library_paths()
    return {
        "music_dir": settings.MUSIC_OUTPUT_DIR,
        "dir_exists": os.path.exists(settings.MUSIC_OUTPUT_DIR),
        "jellyfin_url": settings.JELLYFIN_URL,
        "jellyfin_api_key": settings.JELLYFIN_API_KEY or "",
        "jellyfin_user_id": settings.JELLYFIN_USER_ID or "",
        "language": getattr(settings, "LANGUAGE", "en") or "en",
        "setup_completed": getattr(settings, "SETUP_COMPLETED", False),
        "jellyfin_music_paths": jellyfin_paths
    }

@app.post("/api/settings")
async def save_settings(payload: dict, request: Request):
    """Salva e applica le impostazioni in runtime e persiste su config.json (con priorità) e .env"""
    principal = getattr(request.state, "app_user", None)
    if principal and not principal.get("is_admin"):
        if set(payload) - {"language"}:
            raise HTTPException(status_code=403, detail="Only an administrator can change the global configuration.")
    elif getattr(settings, "SETUP_COMPLETED", False) and not principal:
        raise HTTPException(status_code=401, detail="Sign in as an administrator to manage configuration.")
    save_webapp_config(payload)

    return {
        "success": True,
        "message": "Settings saved and applied successfully.",
        "music_dir": settings.MUSIC_OUTPUT_DIR,
        "dir_exists": os.path.exists(settings.MUSIC_OUTPUT_DIR),
        "language": getattr(settings, "LANGUAGE", "en") or "en",
        "setup_completed": getattr(settings, "SETUP_COMPLETED", False)
    }

@app.post("/api/settings/test-jellyfin")
async def test_jellyfin_connection(payload: dict, request: Request):
    """Verifica se l'URL e l'API Key di Jellyfin sono validi e raggiungibili"""
    if getattr(settings, "SETUP_COMPLETED", False):
        require_admin(request)
    import httpx
    url = (payload.get("jellyfin_url") or settings.JELLYFIN_URL).rstrip('/')
    api_key = payload.get("jellyfin_api_key")
    if api_key is None or api_key == "":
        api_key = settings.JELLYFIN_API_KEY

    if not url:
        return {"success": False, "message": "Enter the Jellyfin server URL."}

    # 1. Test endpoint pubblico (nessuna autenticazione richiesta) per verificare raggiungibilità
    server_info = {}
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            pub_res = await client.get(f"{url}/System/Info/Public")
            if pub_res.status_code == 200:
                server_info = pub_res.json()
            else:
                return {
                    "success": False,
                    "message": f"Il server a {url} ha risposto con codice HTTP {pub_res.status_code}."
                }
        except httpx.ConnectError:
            return {
                "success": False,
                "message": f"Could not reach {url}. Check the IP address, port (for example 8096), and that the server is running."
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"Connection to {url} failed: {str(e)}"
            }

        server_name = server_info.get("ServerName", "Jellyfin")
        version = server_info.get("Version", "")

        if not api_key:
            users = await JellyfinService.get_users(jellyfin_url=url, api_key=None)
            return {
                "success": True,
                "message": f"Server '{server_name}' (v{version}) is online. Enter the API key to authenticate.",
                "users": users
            }

        # 2. Test autenticazione con header completo Jellyfin MediaBrowser
        headers = JellyfinService._headers(api_key=api_key)

        try:
            res = await client.get(f"{url}/System/Info", headers=headers)
            if res.status_code == 200:
                data = res.json()
                s_name = data.get("ServerName", server_name)
                s_ver = data.get("Version", version)
                paths = await JellyfinService.get_music_library_paths(jellyfin_url=url, api_key=api_key)
                users = await JellyfinService.get_users(jellyfin_url=url, api_key=api_key)
                return {
                    "success": True,
                    "message": f"Connessione riuscita a '{s_name}' (v{s_ver})! API Key verificata.",
                    "jellyfin_music_paths": paths,
                    "users": users
                }
            elif res.status_code == 401:
                # Prova fallback su /Items o /Users per API key con scope utente
                items_res = await client.get(f"{url}/Items?limit=1", headers=headers)
                if items_res.status_code == 200:
                    paths = await JellyfinService.get_music_library_paths(jellyfin_url=url, api_key=api_key)
                    users = await JellyfinService.get_users(jellyfin_url=url, api_key=api_key)
                    return {
                        "success": True,
                        "message": f"Connessione riuscita a '{server_name}' (v{version})! API Key verificata.",
                        "jellyfin_music_paths": paths,
                        "users": users
                    }
                return {
                    "success": False,
                    "message": f"Server '{server_name}' is reachable, but the API key is invalid or expired (401 Unauthorized)."
                }
            else:
                return {
                    "success": False,
                    "message": f"Il server ha risposto con codice HTTP {res.status_code}."
                }
        except Exception as e:
            return {
                "success": False,
                "message": f"API key verification failed: {str(e)}"
            }

# === PLAYLIST CSV SYNC ENDPOINTS ===
@app.get("/api/jellyfin/playlist/{playlist_id}/csv-info")
async def get_playlist_csv_info(playlist_id: str, request: Request, owner_user_id: Optional[str] = None):
    principal = require_jellyfin_user(request)
    await resolve_playlist_access(playlist_id, principal, owner_user_id=owner_user_id)
    return PlaylistCSVService.get_csv_info(playlist_id)

@app.post("/api/jellyfin/playlist/{playlist_id}/csv")
async def upload_playlist_csv(playlist_id: str, request: Request, file: UploadFile = File(...), owner_user_id: Optional[str] = None):
    principal = require_jellyfin_user(request)
    await resolve_playlist_access(playlist_id, principal, owner_user_id=owner_user_id)
    content_bytes = await file.read()
    try:
        content_str = content_bytes.decode("utf-8")
    except UnicodeDecodeError:
        content_str = content_bytes.decode("latin-1", errors="replace")
    
    meta = PlaylistCSVService.save_csv(playlist_id, file.filename or "playlist.csv", content_str)
    return {"success": True, "meta": meta}

@app.post("/api/jellyfin/playlist/{playlist_id}/csv-match")
async def match_playlist_csv(playlist_id: str, request: Request, owner_user_id: Optional[str] = None):
    principal = require_jellyfin_user(request)
    _, access_user_id = await resolve_playlist_access(playlist_id, principal, owner_user_id=owner_user_id)
    return await PlaylistCSVService.match_playlist(playlist_id, user_id=access_user_id)

from pydantic import BaseModel

class AddItemsRequest(BaseModel):
    item_ids: List[str]
    owner_user_id: Optional[str] = None

class PlaylistEntryRequest(BaseModel):
    entry_id: str
    owner_user_id: Optional[str] = None

class FavoriteRequest(BaseModel):
    favorite: bool = True

@app.post("/api/jellyfin/playlist/{playlist_id}/add-items")
async def add_items_to_playlist(playlist_id: str, req: AddItemsRequest, request: Request):
    principal = require_jellyfin_user(request)
    _, access_user_id = await resolve_playlist_access(playlist_id, principal, for_mutation=True, owner_user_id=req.owner_user_id)
    return await PlaylistCSVService.add_tracks_to_playlist(playlist_id, req.item_ids, user_id=access_user_id)

@app.delete("/api/jellyfin/playlist/{playlist_id}/items/{entry_id}")
async def remove_item_from_playlist(playlist_id: str, entry_id: str, request: Request, owner_user_id: Optional[str] = None):
    principal = require_jellyfin_user(request)
    _, access_user_id = await resolve_playlist_access(
        playlist_id, principal, for_mutation=True, owner_user_id=owner_user_id
    )
    if not await JellyfinService.remove_from_playlist(playlist_id, entry_id, user_id=access_user_id):
        raise HTTPException(status_code=502, detail="Jellyfin could not remove this playlist entry.")
    return {"success": True}

@app.put("/api/jellyfin/items/{item_id}/favorite")
async def set_jellyfin_favorite(item_id: str, payload: FavoriteRequest, request: Request):
    principal = require_jellyfin_user(request)
    if not await JellyfinService.set_favorite(item_id, principal.get("id"), payload.favorite):
        raise HTTPException(status_code=502, detail="Jellyfin could not update this favorite.")
    return {"success": True, "favorite": payload.favorite}

class QueueDownloadsRequest(BaseModel):
    playlist_name: str
    tracks: List[Dict[str, Any]]
    owner_user_id: Optional[str] = None

@app.post("/api/jellyfin/playlist/{playlist_id}/queue-downloads")
async def queue_playlist_downloads(playlist_id: str, req: QueueDownloadsRequest, request: Request):
    principal = require_app_user(request)
    if playlist_id:
        _, access_user_id = await resolve_playlist_access(playlist_id, principal, for_mutation=True, owner_user_id=req.owner_user_id)
    else:
        access_user_id = None
    return await PlaylistCSVService.queue_tracks_download(playlist_id, req.playlist_name, req.tracks, owner=principal, playlist_user_id=access_user_id)




if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=settings.DEBUG)
