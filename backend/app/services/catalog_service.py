"""Deezer catalog lookups that show what the library is missing: album tracks and an artist's releases."""
import asyncio
import re
import time
from typing import List, Optional

import httpx

from app.services.library_layout import normalize_key, title_key

DEEZER_API = "https://api.deezer.com"
CACHE_TTL = 6 * 3600
EDITION_SUFFIX = re.compile(
    r"\s*(?:[\(\[][^\)\]]*\b(?:deluxe|edition|expanded|remaster(?:ed)?|bonus|version|anniversary)\b[^\)\]]*[\)\]]"
    r"|\s+-\s+(?:single|ep)$)", re.IGNORECASE)

_cache: dict = {}
_cache_lock = asyncio.Lock()


def album_key(title: str) -> str:
    """Album comparison key ignoring edition tags (Deluxe, Remastered, ...) and Apple's " - Single"/" - EP"."""
    return normalize_key(EDITION_SUFFIX.sub("", str(title or "")))


async def _get(path: str, **params) -> dict:
    key = (path, tuple(sorted(params.items())))
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    try:
        async with httpx.AsyncClient(timeout=8.0, headers={"Accept-Language": "en"}) as client:
            res = await client.get(f"{DEEZER_API}{path}", params=params)
        data = res.json() if res.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return {}
    if isinstance(data, dict) and not data.get("error"):
        async with _cache_lock:
            if len(_cache) > 2000:
                _cache.clear()
            _cache[key] = (time.time() + CACHE_TTL, data)
        return data
    return {}


def _cover(album: dict) -> Optional[str]:
    return album.get("cover_xl") or album.get("cover_big") or album.get("cover_medium") or None


def _track_payload(item: dict, album: dict) -> dict:
    return {
        "id": f"deezer:{item['id']}",
        "title": item.get("title") or "",
        "artist": (item.get("artist") or {}).get("name") or (album.get("artist") or {}).get("name") or "",
        "album": album.get("title") or "",
        "duration": item.get("duration") or 0,
        "cover_url": _cover(album),
        "preview_url": item.get("preview") or None,
        "isrc": item.get("isrc"),
        "source": "deezer",
        "track_number": item.get("track_position"),
        "disc_number": item.get("disk_number") or 1,
    }


def _name_candidates(name: str) -> List[str]:
    """The library name plus former names that the artist aliases map to it (Deezer may still use those)."""
    from app.config import settings
    aliases = [old for old, new in (settings.ARTIST_ALIASES or {}).items() if normalize_key(new) == normalize_key(name)]
    return [name, *aliases]


async def find_artists(name: str) -> List[dict]:
    """Deezer artists for the library name and each alias (a renamed artist can have one page per name)."""
    found = []
    for candidate in _name_candidates(name):
        hits = (await _get("/search/artist", q=candidate, limit=8)).get("data") or []
        wanted = normalize_key(candidate)
        # Several Deezer artists share popular names; the most followed one is the real artist.
        exact = [a for a in hits if normalize_key(a.get("name")) == wanted]
        if exact:
            best = max(exact, key=lambda a: a.get("nb_fan") or 0)
            if all(best["id"] != f["id"] for f in found):
                found.append(best)
    return found


async def artist_albums(artist_id: int) -> List[dict]:
    return (await _get(f"/artist/{artist_id}/albums", limit=200)).get("data") or []


async def discography(name: str) -> List[dict]:
    lists = await asyncio.gather(*(artist_albums(a["id"]) for a in await find_artists(name)))
    return [item for items in lists for item in items]


async def find_albums(album: str, artist: str) -> List[dict]:
    """The album artist's Deezer releases matching a library album name: exact titles, else edition-free ones."""
    items = await discography(artist)
    exact = [a for a in items if normalize_key(a.get("title")) == normalize_key(album)]
    matches = exact or [a for a in items if album_key(a.get("title")) == album_key(album)]
    unique = {}
    for item in matches:
        unique.setdefault(item.get("id"), item)
    return list(unique.values())


async def album_tracks(deezer_album_id: int) -> tuple[dict, List[dict]]:
    album = await _get(f"/album/{deezer_album_id}")
    if not album:
        return {}, []
    items = (await _get(f"/album/{deezer_album_id}/tracks", limit=200)).get("data") or []
    info = {
        "id": f"deezer:{album['id']}",
        "title": album.get("title") or "",
        "artist": (album.get("artist") or {}).get("name") or "",
        "cover_url": _cover(album),
        "year": (album.get("release_date") or "")[:4] or None,
        "record_type": album.get("record_type") or "album",
    }
    return info, [_track_payload(item, album) for item in items if item.get("id")]


# Deezer search fallback: a release found by title is trusted only when it holds at least this many of the
# library album's songs, and at least half of them.
FALLBACK_MIN_SHARED = 2
VARIOUS_ARTISTS = {"variousartists", "various", "va", "verschiedeneinterpreten", "artistivari"}


async def search_albums(album: str, artist: str) -> List[dict]:
    """Deezer releases titled like the library album, found by search instead of the artist's discography."""
    found = {}
    for query in (f'album:"{album}"', f"{artist} {album}"):
        for item in (await _get("/search/album", q=query, limit=15)).get("data") or []:
            if item.get("id") and album_key(item.get("title")) == album_key(album):
                found.setdefault(item["id"], item)
    return list(found.values())[:6]


def _remember_alias(deezer_artist: str, library_artist: str):
    """Save `deezer name -> library name` so artist pages and new downloads use the library name too."""
    from app.config import save_artist_aliases, settings
    old, new = (deezer_artist or "").strip(), (library_artist or "").strip()
    if not old or not new or normalize_key(old) == normalize_key(new):
        return
    if normalize_key(old) in VARIOUS_ARTISTS or normalize_key(new) in VARIOUS_ARTISTS:
        return
    aliases = dict(settings.ARTIST_ALIASES or {})
    if any(normalize_key(key) == normalize_key(old) for key in aliases):
        return
    aliases[old] = new
    try:
        save_artist_aliases(aliases)
        print(f"[Catalog] Saved artist alias {old!r} -> {new!r}")
    except (OSError, ValueError) as exc:
        print(f"[Catalog] Could not save artist alias {old!r} -> {new!r}: {exc}")


async def missing_album_tracks(album: str, artist: str, library_tracks: List[dict]) -> dict:
    """Catalog tracks of a library album that have no library copy (matched by title key).

    An album can have several editions with the same title (e.g. a reissue with a bonus first track), whose
    track numbers differ; the edition holding the most library songs wins, then the fuller and newer one.
    When the artist's discography has no edition with any library song (the release is listed under a former
    or alternate artist name on Deezer), the album is searched by title and accepted only when it shares
    enough songs with the library album; its Deezer artist name is then saved as an artist alias.
    """
    have = {title_key(t.get("title")) for t in library_tracks if t.get("title")}

    async def best(candidates: List[dict]) -> tuple:
        editions = await asyncio.gather(*(album_tracks(c["id"]) for c in candidates))

        def rank(index: int):
            info, tracks = editions[index]
            keys = {title_key(t["title"]) for t in tracks}
            return (bool(info), len(keys & have), len(tracks), candidates[index].get("release_date") or "")

        index = max(range(len(candidates)), key=rank)
        return candidates[index], editions[index], rank(index)[1]

    candidates = await find_albums(album, artist)
    chosen = await best(candidates) if candidates else None
    if (not chosen or not chosen[2]) and len(have) >= FALLBACK_MIN_SHARED:
        found = await search_albums(album, artist)
        if found:
            match = await best(found)
            if match[2] >= FALLBACK_MIN_SHARED and match[2] * 2 >= len(have):
                chosen = match
                _remember_alias((match[0].get("artist") or {}).get("name") or match[1][0].get("artist"), artist)
    if not chosen or not chosen[1][0]:
        return {"catalog_album": None, "missing": []}
    info, tracks = chosen[1]
    return {"catalog_album": info, "missing": [t for t in tracks if title_key(t["title"]) not in have]}


async def missing_artist_releases(artist: str, library_tracks: List[dict]) -> dict:
    """The artist's Deezer releases absent from the library: albums and EPs first, then singles, newest first."""
    items = await discography(artist)
    have_albums = {album_key(t.get("album")) for t in library_tracks if t.get("album")}
    have_titles = {title_key(t.get("title")) for t in library_tracks}
    seen, releases = set(), []
    for item in sorted(items, key=lambda a: a.get("release_date") or "", reverse=True):
        kind = item.get("record_type") or "album"
        key = album_key(item.get("title"))
        if not key or key in seen or kind == "compile":
            continue
        seen.add(key)
        # A single whose song is already in the library (on any album) is not missing.
        if key in have_albums or (kind == "single" and title_key(item.get("title")) in have_titles):
            continue
        releases.append({
            "id": f"deezer:{item['id']}",
            "title": item.get("title") or "",
            "cover_url": _cover(item),
            "year": (item.get("release_date") or "")[:4] or None,
            "record_type": kind,
        })
    releases.sort(key=lambda r: r["record_type"] == "single")
    return {"releases": releases}


async def catalog_duration(title: str, artist: str) -> Optional[int]:
    """Length of the catalog recording (Deezer), to suggest a trim that matches the original song."""
    hits = (await _get("/search", q=f"{artist} {title}", limit=10)).get("data") or []
    wanted_title, wanted_artist = title_key(title), normalize_key(artist.split(" feat. ")[0])
    for hit in hits:
        if title_key(hit.get("title")) == wanted_title and normalize_key((hit.get("artist") or {}).get("name")) == wanted_artist:
            return hit.get("duration") or None
    return None
