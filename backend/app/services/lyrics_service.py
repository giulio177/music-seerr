import os
import re
import httpx
from typing import Optional, Dict
from mutagen.id3 import ID3, USLT, ID3NoHeaderError
from mutagen.mp3 import MP3

class LyricsService:
    BASE_URL = "https://lrclib.net/api"

    @classmethod
    async def fetch_lyrics(
        cls,
        title: str,
        artist: str,
        album: Optional[str] = None,
        duration: Optional[int] = None
    ) -> Optional[Dict[str, any]]:
        """
        Recupera testi sincronizzati (LRC) e plain da LRCLIB.
        Usa fallback intelligenti rimuovendo tag superflui come (Radio Edit), (feat.), ecc.
        """
        async with httpx.AsyncClient(timeout=8.0) as client:
            # 1. Prova con parametri esatti
            params = {
                "track_name": title,
                "artist_name": artist
            }
            if album:
                params["album_name"] = album
            if duration and duration > 0:
                params["duration"] = duration

            try:
                res = await client.get(f"{cls.BASE_URL}/get", params=params)
                if res.status_code == 200:
                    data = res.json()
                    if data.get("syncedLyrics") or data.get("plainLyrics") or data.get("instrumental"):
                        return {
                            "synced": data.get("syncedLyrics"),
                            "plain": data.get("plainLyrics"),
                            "instrumental": data.get("instrumental", False)
                        }
            except Exception as e:
                print(f"[LyricsService] Errore /api/get: {e}")

            # 2. Pulizia del titolo da parentesi e feature
            clean_title = re.sub(r'\(.*?\)|\[.*?\]', '', title).strip()
            clean_artist = re.sub(r'\(.*?\)|\[.*?\]', '', artist).strip()
            # Rimuovi eventuali "feat." o "ft."
            clean_title = re.split(r'\s+feat\.?|\s+ft\.?', clean_title, flags=re.IGNORECASE)[0].strip()
            clean_artist = re.split(r'\s+feat\.?|\s+ft\.?', clean_artist, flags=re.IGNORECASE)[0].strip()

            queries_to_try = [
                f"{artist} {title}",
                f"{clean_artist} {clean_title}",
                clean_title
            ]

            for q in set(queries_to_try):
                try:
                    res = await client.get(f"{cls.BASE_URL}/search", params={"q": q})
                    if res.status_code == 200:
                        items = res.json()
                        if items and isinstance(items, list):
                            for item in items:
                                if item.get("syncedLyrics") or item.get("plainLyrics"):
                                    return {
                                        "synced": item.get("syncedLyrics"),
                                        "plain": item.get("plainLyrics"),
                                        "instrumental": item.get("instrumental", False)
                                    }
                except Exception as e:
                    print(f"[LyricsService] Errore /api/search ({q}): {e}")

        return None

    @classmethod
    def save_lrc_file(cls, mp3_path: str, synced_lyrics: str) -> Optional[str]:
        """Salva il file .lrc accanto all'MP3"""
        if not mp3_path or not synced_lyrics:
            return None

        base, _ = os.path.splitext(mp3_path)
        lrc_path = f"{base}.lrc"
        try:
            with open(lrc_path, "w", encoding="utf-8") as f:
                f.write(synced_lyrics)
            print(f"[LyricsService] File LRC salvato: {lrc_path}")
            return lrc_path
        except Exception as e:
            print(f"[LyricsService] Errore scrittura LRC: {e}")
            return None

    @classmethod
    def embed_lyrics_id3(cls, mp3_path: str, plain_lyrics: str):
        """Inietta il testo nei metadati ID3"""
        if not mp3_path or not os.path.exists(mp3_path) or not plain_lyrics:
            return

        try:
            try:
                audio = MP3(mp3_path, ID3=ID3)
            except ID3NoHeaderError:
                audio = MP3(mp3_path)
                audio.add_tags()

            audio.tags.add(
                USLT(
                    encoding=3,
                    lang='ita',
                    desc='Lyrics',
                    text=plain_lyrics
                )
            )
            audio.save()
            print(f"[LyricsService] Testo ID3 iniettato in: {mp3_path}")
        except Exception as e:
            print(f"[LyricsService] Errore ID3: {e}")

    @classmethod
    async def get_or_fetch(cls, full_path: str, artist: str, title: str) -> Optional[Dict[str, any]]:
        """
        1. Cerca il file .lrc sul disco nella stessa cartella dell'audio.
        2. Se non esiste, lo cerca online su LRCLIB, lo salva sul disco e lo inietta nell'MP3.
        3. Restituisce il testo (synced e plain) con indicazione della provenienza.
        """
        base, _ = os.path.splitext(full_path)
        lrc_path = f"{base}.lrc"

        # 1. Controlla prima sul disco
        if os.path.exists(lrc_path):
            try:
                with open(lrc_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                return {
                    "source": "disk",
                    "synced": content,
                    "plain": None,
                    "instrumental": False,
                    "lrc_path": lrc_path
                }
            except Exception as e:
                print(f"[LyricsService] Errore lettura LRC da disco: {e}")

        # 2. Se non è presente su disco, scarica online
        online_data = await cls.fetch_lyrics(title, artist)
        if online_data:
            if online_data.get("synced"):
                cls.save_lrc_file(full_path, online_data["synced"])
            if online_data.get("plain"):
                cls.embed_lyrics_id3(full_path, online_data["plain"])
            online_data["source"] = "downloaded"
            online_data["lrc_path"] = lrc_path
            return online_data

        return None
