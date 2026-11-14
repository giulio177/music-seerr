"""Lidarr-compatible library layout and release metadata resolution.

Tracks are stored as:
    {Album Artist}/{Album Title} ({Year})/{Album Artist} - {Album Title} - {NN} - {Track Title}.{ext}
which matches Lidarr's default naming, so downloaded singles sit next to the existing library.
"""

import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

import httpx

from app.models.schemas import TrackItem

AUDIO_EXTENSIONS = (".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav")
DEEZER_API = "https://api.deezer.com"
ITUNES_LOOKUP = "https://itunes.apple.com/lookup"
YEAR_SUFFIX = re.compile(r"\s*[\(\[]\d{4}[\)\]]\s*$")


def sanitize_component(name: str, max_length: int = 150) -> str:
    """Convert metadata into one safe filesystem component."""
    safe = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "", str(name or "")).strip().strip(".")
    safe = re.sub(r"\.{2,}", ".", safe)
    safe = re.sub(r"\s{2,}", " ", safe)
    return safe[:max_length].strip() or "Unknown"


def normalize_key(text: str) -> str:
    """Comparison key that ignores case, accents, punctuation, and dash/quote variants."""
    text = unicodedata.normalize("NFKD", str(text or "")).casefold()
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("&", "and")
    # Keep letters of every script (e.g. Japanese titles); drop punctuation and spacing.
    return re.sub(r"[\W_]+", "", text)


@dataclass
class ReleaseInfo:
    title: str
    artist: str
    album_artist: str
    album: str
    year: Optional[int] = None
    track_number: Optional[int] = None
    disc_number: Optional[int] = None
    isrc: Optional[str] = None
    cover_url: Optional[str] = None
    matched: bool = False
    extra: dict = field(default_factory=dict)


def _year(date: Optional[str]) -> Optional[int]:
    match = re.match(r"(\d{4})", str(date or ""))
    year = int(match.group(1)) if match else None
    return year if year and 1900 <= year <= 2100 else None


def _positive(value) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


async def _deezer_track(client: httpx.AsyncClient, track_id: str, fallback: TrackItem) -> Optional[ReleaseInfo]:
    res = await client.get(f"{DEEZER_API}/track/{track_id}")
    data = res.json() if res.status_code == 200 else {}
    if not data or data.get("error"):
        return None
    album = data.get("album") or {}
    album_artist = ""
    year = _year(data.get("release_date"))
    if album.get("id"):
        album_res = await client.get(f"{DEEZER_API}/album/{album['id']}")
        album_data = album_res.json() if album_res.status_code == 200 else {}
        if album_data and not album_data.get("error"):
            album_artist = (album_data.get("artist") or {}).get("name", "")
            year = _year(album_data.get("release_date")) or year
    artist = (data.get("artist") or {}).get("name") or fallback.artist
    return ReleaseInfo(
        title=data.get("title") or fallback.title,
        artist=artist,
        album_artist=album_artist or artist,
        album=album.get("title") or fallback.album,
        year=year,
        track_number=_positive(data.get("track_position")),
        disc_number=_positive(data.get("disk_number")),
        isrc=data.get("isrc") or fallback.isrc,
        cover_url=fallback.cover_url or album.get("cover_xl") or album.get("cover_big"),
        matched=True,
    )


async def _itunes_track(client: httpx.AsyncClient, track_id: str, fallback: TrackItem) -> Optional[ReleaseInfo]:
    res = await client.get(ITUNES_LOOKUP, params={"id": track_id, "country": "IT"})
    results = (res.json() or {}).get("results", []) if res.status_code == 200 else []
    data = next((item for item in results if item.get("wrapperType") == "track"), None)
    if not data:
        return None
    artist = data.get("artistName") or fallback.artist
    return ReleaseInfo(
        title=data.get("trackName") or fallback.title,
        artist=artist,
        album_artist=data.get("collectionArtistName") or artist,
        album=re.sub(r"\s+-\s+(Single|EP)$", "", data.get("collectionName") or "") or fallback.album,
        year=_year(data.get("releaseDate")),
        track_number=_positive(data.get("trackNumber")),
        disc_number=_positive(data.get("discNumber")),
        isrc=fallback.isrc,
        cover_url=fallback.cover_url,
        matched=True,
    )


def _match_score(track: TrackItem, title: str, short_title: str, artist: str, album: str) -> int:
    """Rank a search hit; an exact title beats another version that only shares the short title."""
    title_key, artist_key, album_key = normalize_key(track.title), normalize_key(track.artist), normalize_key(track.album)
    value = 0
    if normalize_key(title) == title_key:
        value += 5
    elif short_title and normalize_key(short_title) == title_key:
        value += 2
    if normalize_key(artist) == artist_key:
        value += 2
    item_album = normalize_key(album)
    if album_key and item_album == album_key:
        value += 3
    elif album_key and item_album and (album_key in item_album or item_album in album_key):
        value += 1
    return value


MIN_MATCH_SCORE = 6


async def _deezer_search(client: httpx.AsyncClient, track: TrackItem) -> Optional[ReleaseInfo]:
    """Find the release on Deezer by artist and title when no provider ID is available."""
    plain = re.sub(r"[()\[\]\-\"]+", " ", f"{track.artist} {track.title}")
    items = []
    for query in (plain, f'artist:"{track.artist}" track:"{track.title}"'):
        res = await client.get(f"{DEEZER_API}/search", params={"q": query, "limit": 10})
        items += (res.json() or {}).get("data", []) if res.status_code == 200 else []

    def score(item: dict) -> int:
        return _match_score(track, item.get("title"), item.get("title_short"),
                            (item.get("artist") or {}).get("name"), (item.get("album") or {}).get("title"))

    ranked = sorted(items, key=score, reverse=True)
    if not ranked or score(ranked[0]) < MIN_MATCH_SCORE:
        return None
    return await _deezer_track(client, str(ranked[0]["id"]), track)


async def _itunes_search(client: httpx.AsyncClient, track: TrackItem) -> Optional[ReleaseInfo]:
    """Second catalog for tracks that Deezer search does not surface."""
    res = await client.get("https://itunes.apple.com/search",
                           params={"term": f"{track.artist} {track.title}", "entity": "song", "limit": 15, "country": "IT"})
    items = (res.json() or {}).get("results", []) if res.status_code == 200 else []

    def score(item: dict) -> int:
        return _match_score(track, item.get("trackName"), item.get("trackCensoredName"),
                            item.get("artistName"), item.get("collectionName"))

    ranked = sorted(items, key=score, reverse=True)
    if not ranked or score(ranked[0]) < MIN_MATCH_SCORE:
        return None
    return await _itunes_track(client, str(ranked[0]["trackId"]), track)


async def resolve_release(track: TrackItem) -> ReleaseInfo:
    """Resolve album artist, year, and track position for a track, falling back to its own fields."""
    source, _, provider_id = (track.id or "").partition(":")
    try:
        # English names keep folder names stable (e.g. "Various Artists", not a localized label).
        async with httpx.AsyncClient(timeout=8.0, headers={"Accept-Language": "en"}) as client:
            info = None
            if source == "deezer" and provider_id.isdigit():
                info = await _deezer_track(client, provider_id, track)
            elif source == "itunes" and provider_id.isdigit():
                info = await _itunes_track(client, provider_id, track)
            if not info:
                info = await _deezer_search(client, track) or await _itunes_search(client, track)
            if info:
                return info
    except (httpx.HTTPError, ValueError) as exc:
        print(f"[LibraryLayout] Metadata lookup failed for {track.artist} - {track.title}: {exc}")
    return ReleaseInfo(
        title=track.title,
        artist=track.artist,
        album_artist=track.artist,
        album=track.album or track.title,
        isrc=track.isrc,
        cover_url=track.cover_url,
    )


def _existing_child(parent: str, wanted: str, ignore_year: bool = False) -> Optional[str]:
    """Reuse an existing folder whose name matches ignoring case, punctuation, and a year suffix."""
    key = normalize_key(YEAR_SUFFIX.sub("", wanted) if ignore_year else wanted)
    try:
        entries = sorted(os.scandir(parent), key=lambda entry: entry.name)
    except OSError:
        return None
    for entry in entries:
        if not entry.is_dir():
            continue
        name = YEAR_SUFFIX.sub("", entry.name) if ignore_year else entry.name
        if normalize_key(name) == key:
            return entry.name
    return None


def track_stem(info: ReleaseInfo) -> str:
    artist = sanitize_component(info.album_artist, 80)
    album = sanitize_component(info.album, 80)
    title = sanitize_component(info.title, 120)
    if info.track_number:
        return f"{artist} - {album} - {info.track_number:02d} - {title}"
    return f"{artist} - {album} - {title}"


def album_directory(music_root: str, info: ReleaseInfo) -> str:
    """Return the absolute album folder, reusing matching artist/album folders already on disk."""
    root = os.path.realpath(music_root)
    artist_name = sanitize_component(info.album_artist)
    artist_dir = _existing_child(root, artist_name) or artist_name
    album_label = f"{info.album} ({info.year})" if info.year else info.album
    album_name = sanitize_component(album_label)
    album_dir = _existing_child(os.path.join(root, artist_dir), album_name, ignore_year=True) or album_name
    path = os.path.realpath(os.path.join(root, artist_dir, album_dir))
    if os.path.commonpath([root, path]) != root or path == root:
        raise ValueError("Invalid destination path")
    return path


def find_existing_track(album_dir: str, info: ReleaseInfo) -> Optional[str]:
    """Return an audio file already in the album folder for the same track title."""
    title_key = normalize_key(info.title)
    if not title_key:
        return None
    try:
        entries = list(os.scandir(album_dir))
    except OSError:
        return None
    for entry in entries:
        stem, ext = os.path.splitext(entry.name)
        if ext.lower() not in AUDIO_EXTENSIONS or not entry.is_file():
            continue
        last_part = stem.rsplit(" - ", 1)[-1]
        if normalize_key(last_part) == title_key or normalize_key(stem) == title_key:
            return entry.path
    return None
