"""Personal "Picked for you" feed built from a user's listening history and own Jellyfin playlists."""
import asyncio
import random
import time
from collections import Counter
from typing import Iterable, List, Optional

import httpx

from app.models.schemas import TrackItem
from app.services.library_layout import normalize_key

DEEZER_API = "https://api.deezer.com"
PLAY_HALF_LIFE_DAYS = 14
PLAYLIST_TRACK_WEIGHT = 0.5


SKIP_RATIO = 0.25
SKIPPED_PLAYLIST_WEIGHT = 0.15


def listen_ratio(play: dict) -> Optional[float]:
    """Share of the track actually heard, or None for plays recorded before listening time was tracked."""
    duration = float(play.get("duration") or 0)
    listened = float(play.get("listened_seconds") or 0)
    if duration <= 0 or (listened <= 0 and not play.get("updated_at")):
        return None
    return min(1.0, listened / duration)


def play_signal(ratio: Optional[float], in_playlist: bool) -> float:
    """How much one play says about liking the artist.

    Full listens count 1, partial ones less. A skip (under a quarter heard) is a mild negative signal,
    except for a song the user saved in a playlist: they chose it, so a skip there only means less interest now.
    """
    if ratio is None:
        return 1.0
    if ratio >= 0.8:
        return 1.0
    if ratio >= 0.5:
        return 0.75
    if ratio >= SKIP_RATIO:
        return 0.4
    return 0.0 if in_playlist else -0.3


def artist_weights(plays: Iterable[dict], playlist_tracks: Iterable[dict], now: Optional[float] = None) -> Counter:
    """Score artists from plays (scaled by how much was heard, halving every two weeks) and playlist entries.

    Playlist entries count 0.5 each, but a playlist song the user keeps skipping lately and never finishes
    counts only 0.15: their taste is drifting away from it.
    """
    now = now or time.time()
    plays = list(plays)
    playlist_tracks = list(playlist_tracks)
    playlist_keys = {track_key(t.get("title", ""), t.get("artist", "")) for t in playlist_tracks}
    skips: Counter = Counter()
    full_listens: set = set()
    weights: Counter = Counter()
    for play in plays:
        artist = (play.get("artist") or "").strip()
        if not artist:
            continue
        key = track_key(play.get("title", ""), artist)
        ratio = listen_ratio(play)
        age_days = max(0.0, (now - float(play.get("played_at") or now)) / 86400)
        if age_days <= 30 and ratio is not None:
            if ratio < SKIP_RATIO:
                skips[key] += 1
            elif ratio >= 0.8:
                full_listens.add(key)
        weights[artist] += play_signal(ratio, key in playlist_keys) * 0.5 ** (age_days / PLAY_HALF_LIFE_DAYS)
    for track in playlist_tracks:
        key = track_key(track.get("title", ""), track.get("artist", ""))
        drifting = skips[key] >= 2 and key not in full_listens
        for artist in track.get("artists") or [track.get("artist")]:
            if artist and artist.strip():
                weights[artist.strip()] += SKIPPED_PLAYLIST_WEIGHT if drifting else PLAYLIST_TRACK_WEIGHT
    # Artists only ever skipped end up at zero or below and are not used as seeds.
    return Counter({artist: weight for artist, weight in weights.items() if weight > 0})


def track_key(title: str, artist: str) -> str:
    return f"{normalize_key(artist)}|{normalize_key(title)}"


def _deezer_track(item: dict) -> Optional[TrackItem]:
    if not item.get("id") or not item.get("title"):
        return None
    album = item.get("album") or {}
    return TrackItem(
        id=f"deezer:{item['id']}",
        title=item["title"][:300],
        artist=((item.get("artist") or {}).get("name") or "Unknown artist")[:200],
        album=(album.get("title") or "")[:200],
        duration=item.get("duration") or 0,
        cover_url=album.get("cover_xl") or album.get("cover_big") or album.get("cover_medium") or None,
        preview_url=item.get("preview") or None,
        source="deezer",
    )


async def _get(client: httpx.AsyncClient, path: str, **params) -> list:
    try:
        res = await client.get(f"{DEEZER_API}{path}", params=params)
        data = res.json() if res.status_code == 200 else {}
        return data.get("data", []) if isinstance(data, dict) else []
    except (httpx.HTTPError, ValueError):
        return []


async def _picks_for_seed(client: httpx.AsyncClient, seed: str, rng: random.Random) -> List[TrackItem]:
    """Two tracks from artists related to the seed, then one deeper cut from the seed itself."""
    hits = await _get(client, "/search/artist", q=seed, limit=5)
    artist = next((a for a in hits if normalize_key(a.get("name")) == normalize_key(seed)), None)
    if not artist:
        return []
    related = await _get(client, f"/artist/{artist['id']}/related", limit=12)
    chosen = rng.sample(related, min(2, len(related)))
    tops = await asyncio.gather(*(_get(client, f"/artist/{a['id']}/top", limit=6) for a in chosen),
                                _get(client, f"/artist/{artist['id']}/top", limit=15))
    picks: List[TrackItem] = []
    for items in tops[:-1]:
        if items:
            picks.append(_deezer_track(rng.choice(items[:4])))
    own = tops[-1][3:] or tops[-1]
    if own:
        picks.append(_deezer_track(rng.choice(own)))
    return [p for p in picks if p]


async def build_feed(user_id: str, weights: Counter, known: set, limit: int) -> tuple[List[str], List[TrackItem]]:
    """Pick seed artists (weighted, stable per user per day) and collect fresh tracks around them."""
    if not weights:
        return [], []
    rng = random.Random(f"{user_id}:{time.strftime('%Y-%m-%d')}")
    pool = [name for name, _ in weights.most_common(16)]
    seeds: List[str] = []
    candidates = {name: weights[name] for name in pool}
    while candidates and len(seeds) < 5:
        name = rng.choices(list(candidates), weights=list(candidates.values()))[0]
        seeds.append(name)
        candidates.pop(name)
    async with httpx.AsyncClient(timeout=8.0, headers={"Accept-Language": "en"}) as client:
        groups = await asyncio.gather(*(_picks_for_seed(client, seed, rng) for seed in seeds))
    feed: List[TrackItem] = []
    seen = set(known)
    # Interleave so one seed artist never fills the first row.
    for row in range(max((len(g) for g in groups), default=0)):
        for group in groups:
            if row < len(group):
                key = track_key(group[row].title, group[row].artist)
                if key not in seen:
                    seen.add(key)
                    feed.append(group[row])
    return seeds, feed[:limit]
