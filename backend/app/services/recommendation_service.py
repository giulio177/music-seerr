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


def artist_weights(plays: Iterable[dict], playlist_tracks: Iterable[dict], now: Optional[float] = None) -> Counter:
    """Score artists: each play counts 1, halving every two weeks; each playlist entry counts 0.5."""
    now = now or time.time()
    weights: Counter = Counter()
    for play in plays:
        artist = (play.get("artist") or "").strip()
        if artist:
            age_days = max(0.0, (now - float(play.get("played_at") or now)) / 86400)
            weights[artist] += 0.5 ** (age_days / PLAY_HALF_LIFE_DAYS)
    for track in playlist_tracks:
        for artist in track.get("artists") or [track.get("artist")]:
            if artist and artist.strip():
                weights[artist.strip()] += PLAYLIST_TRACK_WEIGHT
    return weights


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
