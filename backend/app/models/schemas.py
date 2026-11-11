from pydantic import BaseModel, Field
from typing import Optional, List
import time

class TrackItem(BaseModel):
    id: str = Field(..., max_length=256)
    title: str = Field(..., max_length=300)
    artist: str = Field(..., max_length=200)
    album: str = Field(..., max_length=200)
    duration: int = Field(default=0, ge=0, le=86400)
    cover_url: Optional[str] = Field(default=None, max_length=2048)
    preview_url: Optional[str] = Field(default=None, max_length=2048)
    isrc: Optional[str] = Field(default=None, max_length=32)
    source: str = Field(default="deezer", max_length=40)
    in_jellyfin: Optional[bool] = False
    jellyfin_id: Optional[str] = None

class SearchResponse(BaseModel):
    query: str
    total: int
    results: List[TrackItem]

class DownloadRequest(BaseModel):
    track: TrackItem
    playlist_id: Optional[str] = None
    playlist_name: Optional[str] = None
    owner_user_id: Optional[str] = None

class DownloadJob(BaseModel):
    job_id: str
    track: TrackItem
    playlist_id: Optional[str] = None
    playlist_name: Optional[str] = None
    playlist_user_id: Optional[str] = None
    status: str = "pending"  # pending, downloading, tagging, moving, jellyfin_sync, completed, failed
    progress: int = 0
    message: Optional[str] = None
    file_path: Optional[str] = None
    owner_user_id: Optional[str] = None
    owner_name: Optional[str] = None
    created_at: float = Field(default_factory=time.time)

class JellyfinPlaylist(BaseModel):
    id: str
    name: str
    track_count: Optional[int] = 0
    cover_url: Optional[str] = None
    owner_user_id: Optional[str] = None
    owner_name: Optional[str] = None
