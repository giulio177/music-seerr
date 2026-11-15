"""Lidarr-compatible library layout and release metadata resolution.

Tracks are stored as:
    {Album Artist}/{Album Title} ({Year})/{Album Artist} - {Album Title} - {NN} - {Track Title}.{ext}
which matches Lidarr's default naming, so downloaded singles sit next to the existing library.
"""

import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import List, Optional

import httpx
import mutagen

from app.config import settings
from app.models.schemas import TrackItem

AUDIO_EXTENSIONS = (".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav")
DEEZER_API = "https://api.deezer.com"
ITUNES_LOOKUP = "https://itunes.apple.com/lookup"
YEAR_SUFFIX = re.compile(r"\s*[\(\[]\d{4}[\)\]]\s*$")
FEATURING = re.compile(r"\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s[^\)\]]*[\)\]]", re.IGNORECASE)
# "Main feat. Guest" inside an artist credit; the main part is never split further (bands use "&").
ARTIST_FEATURING = re.compile(r"\s+[\(\[]?\s*(?:feat\.?|ft\.?|featuring)\s+", re.IGNORECASE)
GUEST_SEPARATOR = re.compile(r"\s*[,&]\s*")


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


def split_artists(credit: str) -> List[str]:
    """Split "Main feat. Guest A & Guest B" into ["Main", "Guest A", "Guest B"]."""
    credit = str(credit or "").strip()
    parts = ARTIST_FEATURING.split(credit, maxsplit=1)
    names = [parts[0].strip()]
    if len(parts) > 1:
        names += GUEST_SEPARATOR.split(parts[1].strip().rstrip(")]").strip())
    return unique_artists(names)


def unique_artists(names) -> List[str]:
    """Drop empty and repeated names (case-insensitive), keeping order, and apply configured aliases."""
    seen, result = set(), []
    for name in names:
        name = canonical_artist(str(name or "").strip())
        key = normalize_key(name)
        if name and key not in seen:
            seen.add(key)
            result.append(name)
    return result


def canonical_artist(name: str) -> str:
    """Map an artist's former or alternate name (config `artist_aliases`) to the library name."""
    aliases = {normalize_key(alias): target for alias, target in (settings.ARTIST_ALIASES or {}).items()}
    return aliases.get(normalize_key(name), name)


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
    artists: List[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        # Track artists always start with the main artist; guests follow.
        self.artists = unique_artists(self.artists or split_artists(self.artist))
        self.artist = self.artists[0] if self.artists else canonical_artist(self.artist)
        self.album_artist = canonical_artist(split_artists(self.album_artist)[0] if self.album_artist else self.artist)

    @property
    def artist_credit(self) -> str:
        """Display credit such as "Main feat. Guest A, Guest B"."""
        if len(self.artists) < 2:
            return self.artist
        return f"{self.artists[0]} feat. {', '.join(self.artists[1:])}"


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
    contributors = data.get("contributors") or []
    roles = {"Main": 0, "Featured": 1}
    credited = [c.get("name") for c in sorted(contributors, key=lambda c: roles.get(c.get("role"), 2))
                if c.get("role") in roles]
    return ReleaseInfo(
        title=data.get("title") or fallback.title,
        artist=artist,
        artists=[*split_artists(artist), *credited],
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
        cover_url=fallback.cover_url or (data.get("artworkUrl100") or "").replace("100x100bb", "1000x1000bb") or None,
        matched=True,
    )


def _match_score(track: TrackItem, title: str, short_title: str, artist: str, album: str) -> int:
    """Rank a search hit; an exact title beats another version that only shares the short title."""
    title_key, artist_key, album_key = normalize_key(track.title), normalize_key(track.artist), normalize_key(track.album)
    value = 0
    if normalize_key(title) == title_key:
        value += 5
    elif normalize_key(FEATURING.sub("", title or "")) == normalize_key(FEATURING.sub("", track.title or "")):
        # "Song (feat. Someone)" is the same recording as "Song".
        value += 4
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
    if not key:
        return None  # Symbol-only names ("+", "÷") have no comparable key; never reuse another folder.
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


def album_directory(music_root: str, info: ReleaseInfo, planned: Optional[dict] = None) -> str:
    """Return the absolute album folder, reusing matching artist/album folders already on disk.

    `planned` collects folders chosen earlier in the same batch, so tracks of one album that report
    different years (single vs. album release) still share one folder. When a folder is reused, the
    track takes that folder's year so every track of the album carries the same year tag.
    """
    root = os.path.realpath(music_root)
    artist_name = sanitize_component(info.album_artist)
    artist_key = normalize_key(artist_name)
    album_key = normalize_key(sanitize_component(info.album))
    planned = planned if planned is not None else {}
    artist_dir = (planned.get((artist_key, None)) or _existing_child(root, artist_name) or artist_name)
    album_label = f"{info.album} ({info.year})" if info.year else info.album
    album_name = sanitize_component(album_label)
    album_dir = ((planned.get((artist_key, album_key)) if album_key else None)
                 or _existing_child(os.path.join(root, artist_dir), album_name, ignore_year=True) or album_name)
    planned[(artist_key, None)] = artist_dir
    if album_key:
        planned[(artist_key, album_key)] = album_dir
    folder_year = re.search(r"[\(\[](\d{4})[\)\]]\s*$", album_dir)
    if folder_year:
        info.year = int(folder_year.group(1))
    path = os.path.realpath(os.path.join(root, artist_dir, album_dir))
    if os.path.commonpath([root, path]) != root or path == root:
        raise ValueError("Invalid destination path")
    return path


def find_existing_tracks(album_dir: str, info: ReleaseInfo) -> List[str]:
    """Return every audio file already in the album folder for the same track title."""
    title_key = normalize_key(info.title)
    if not title_key:
        return []
    try:
        entries = sorted(os.scandir(album_dir), key=lambda entry: entry.name)
    except OSError:
        return []
    found = []
    for entry in entries:
        stem, ext = os.path.splitext(entry.name)
        if ext.lower() not in AUDIO_EXTENSIONS or not entry.is_file():
            continue
        last_part = stem.rsplit(" - ", 1)[-1]
        if normalize_key(last_part) == title_key or normalize_key(stem) == title_key \
                or normalize_key(_tag_title(entry.path)) == title_key:
            found.append(entry.path)
    return found


def _tag_title(path: str) -> str:
    """Title tag of a file whose name does not follow the library naming."""
    try:
        audio = mutagen.File(path, easy=True)
        title = (audio.tags or {}).get("title") if audio is not None else None
        return str(title[0] if isinstance(title, list) and title else title or "")
    except Exception:
        return ""


def find_existing_track(album_dir: str, info: ReleaseInfo) -> Optional[str]:
    """Return an audio file already in the album folder for the same track title."""
    found = find_existing_tracks(album_dir, info)
    return found[0] if found else None


LOSSLESS_CODECS = ("flac", "alac", "wav", "pcm", "aiff")


def audio_quality(path: str) -> dict:
    """Describe a file's audio resolution; `score` orders lossless above lossy, then bit depth, rate, bitrate."""
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    lossless, bits, rate, bitrate, codec = False, 0, 0, 0, ext
    try:
        audio = mutagen.File(path)
        info = getattr(audio, "info", None)
        if info is not None:
            codec = str(getattr(info, "codec", "") or ext).lower()
            bits = int(getattr(info, "bits_per_sample", 0) or 0)
            rate = int(getattr(info, "sample_rate", 0) or 0)
            bitrate = int(getattr(info, "bitrate", 0) or 0)
            length = float(getattr(info, "length", 0) or 0)
            if not bitrate and length:
                bitrate = int(os.path.getsize(path) * 8 / length)
    except Exception:
        pass
    lossless = ext in ("flac", "wav", "aiff", "aif") or any(name in codec for name in LOSSLESS_CODECS)
    if lossless:
        label = f"{('ALAC' if 'alac' in codec else ext.upper())} {bits or 16}-bit / {rate / 1000:g} kHz" if rate else ext.upper()
    else:
        label = f"{ext.upper()} {round(bitrate / 1000)} kbps" if bitrate else ext.upper()
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return {"label": label, "score": [int(lossless), bits, rate, bitrate, size]}
