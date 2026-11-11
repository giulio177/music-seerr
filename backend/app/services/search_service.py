import httpx
import asyncio
from typing import List, Optional
from app.models.schemas import TrackItem

class SearchService:
    @staticmethod
    async def search_deezer(query: str, limit: int = 25) -> List[TrackItem]:
        """Cerca su Deezer (MP3 preview a 30s e cover HD)"""
        url = "https://api.deezer.com/search"
        params = {"q": query, "limit": limit}
        
        async with httpx.AsyncClient(timeout=8.0) as client:
            try:
                response = await client.get(url, params=params)
                if response.status_code != 200:
                    return []
                data = response.json()
                items = data.get("data", [])
                
                results: List[TrackItem] = []
                for item in items:
                    cover = (
                        item.get("album", {}).get("cover_xl") or
                        item.get("album", {}).get("cover_big") or
                        item.get("album", {}).get("cover_medium") or
                        ""
                    )
                    preview = item.get("preview")
                    results.append(
                        TrackItem(
                            id=f"deezer:{item.get('id')}",
                            title=item.get("title", "Unknown track"),
                            artist=item.get("artist", {}).get("name", "Unknown artist"),
                            album=item.get("album", {}).get("title", "Unknown album"),
                            duration=item.get("duration", 0),
                            cover_url=cover,
                            preview_url=preview if preview else None,
                            isrc=item.get("isrc"),
                            source="deezer"
                        )
                    )
                return results
            except Exception as e:
                print(f"[SearchService] Errore ricerca Deezer: {e}")
                return []

    @staticmethod
    async def search_itunes(query: str, limit: int = 25) -> List[TrackItem]:
        """Cerca su iTunes / Apple Music (AAC preview a 30s ufficiale)"""
        url = "https://itunes.apple.com/search"
        params = {"term": query, "entity": "song", "limit": limit, "country": "IT"}
        
        async with httpx.AsyncClient(timeout=8.0) as client:
            try:
                response = await client.get(url, params=params)
                if response.status_code != 200:
                    return []
                data = response.json()
                items = data.get("results", [])
                
                results: List[TrackItem] = []
                for item in items:
                    artwork = item.get("artworkUrl100", "")
                    if artwork:
                        artwork = artwork.replace("100x100bb", "600x600bb")
                    
                    duration_sec = int(item.get("trackTimeMillis", 0) / 1000)
                    preview = item.get("previewUrl")
                    results.append(
                        TrackItem(
                            id=f"itunes:{item.get('trackId')}",
                            title=item.get("trackName", "Unknown track"),
                            artist=item.get("artistName", "Unknown artist"),
                            album=item.get("collectionName", "Unknown album"),
                            duration=duration_sec,
                            cover_url=artwork,
                            preview_url=preview if preview else None,
                            source="itunes"
                        )
                    )
                return results
            except Exception as e:
                print(f"[SearchService] Errore ricerca iTunes: {e}")
                return []

    @classmethod
    async def get_itunes_preview_fallback(cls, artist: str, title: str) -> Optional[str]:
        """Cerca velocemente su iTunes per trovare una preview se Deezer non l'ha fornita"""
        try:
            itunes_results = await cls.search_itunes(f"{artist} {title}", limit=3)
            for track in itunes_results:
                if track.preview_url:
                    return track.preview_url
        except Exception:
            pass
        return None

    @classmethod
    async def search(cls, query: str, limit: int = 25) -> List[TrackItem]:
        """
        Ricerca ibrida avanzata: interroga Deezer e iTunes in parallelo.
        Se un brano su Deezer non ha la preview audio, recupera la preview da iTunes!
        """
        deezer_task = cls.search_deezer(query, limit)
        itunes_task = cls.search_itunes(query, limit)
        
        deezer_results, itunes_results = await asyncio.gather(deezer_task, itunes_task)
        
        # Mappa iTunes per lookup veloce di preview e tracce mancanti
        itunes_by_key = {}
        for it in itunes_results:
            key = f"{it.artist.lower()} - {it.title.lower()}"
            itunes_by_key[key] = it

        results = deezer_results if deezer_results else itunes_results

        # Per ogni traccia Deezer senza preview_url, prova ad abbinare iTunes
        for track in results:
            if not track.preview_url:
                # Controlla per matching approssimativo in itunes_results
                found_preview = None
                for it in itunes_results:
                    if (track.title.lower() in it.title.lower() or it.title.lower() in track.title.lower()) and it.preview_url:
                        found_preview = it.preview_url
                        break
                if found_preview:
                    track.preview_url = found_preview

        # Se Deezer ha restituito pochissimi risultati (< 3) e iTunes ne ha di più, unisci i risultati non duplicati
        if len(results) < limit and itunes_results:
            existing_titles = {t.title.lower() for t in results}
            for it in itunes_results:
                if it.title.lower() not in existing_titles:
                    results.append(it)
                    existing_titles.add(it.title.lower())
                if len(results) >= limit:
                    break

        return results
