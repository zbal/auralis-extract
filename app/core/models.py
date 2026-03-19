from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Optional


STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_FAILED = "failed"
STATUS_COMPLETED = "completed"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobRecord:
    job_id: str
    input_url: str
    status: str
    created_at: str
    stage: str = "queued"
    progress: int = 0
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    message: Optional[str] = None
    output_filename: Optional[str] = None
    source_video_id: Optional[str] = None
    media_title: Optional[str] = None
    media_artist: Optional[str] = None
    media_thumbnail_url: Optional[str] = None
    thumbnail_embedded: bool = False
    loudness_target: str = "-16 LUFS"
    bitrate: str = "128k"
    error_code: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "JobRecord":
        allowed = cls.__dataclass_fields__.keys()
        filtered = {k: v for k, v in payload.items() if k in allowed}
        return cls(**filtered)


@dataclass
class PlaylistStreamRecord:
    video_id: str
    url: str
    title: str = "Unknown Title"
    artist: str = "Unknown Artist"
    thumbnail_url: str = ""
    position: Optional[int] = None
    availability: str = ""
    availability_status: str = "available"
    availability_reason: str = ""
    availability_last_checked_at: str = ""
    discovered_sync_revision: int = 0
    last_seen_sync_revision: int = 0
    last_job_id: str = ""
    last_queue_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict) -> "PlaylistStreamRecord":
        allowed = cls.__dataclass_fields__.keys()
        filtered = {k: v for k, v in payload.items() if k in allowed}
        filtered["video_id"] = str(filtered.get("video_id") or "")
        filtered["url"] = str(filtered.get("url") or "")
        filtered["title"] = str(filtered.get("title") or "Unknown Title")
        filtered["artist"] = str(filtered.get("artist") or "Unknown Artist")
        filtered["thumbnail_url"] = str(filtered.get("thumbnail_url") or "")
        filtered["availability"] = str(filtered.get("availability") or "")
        filtered["availability_status"] = str(filtered.get("availability_status") or "available")
        filtered["availability_reason"] = str(filtered.get("availability_reason") or "")
        filtered["availability_last_checked_at"] = str(filtered.get("availability_last_checked_at") or "")
        filtered["discovered_sync_revision"] = int(filtered.get("discovered_sync_revision") or 0)
        filtered["last_seen_sync_revision"] = int(filtered.get("last_seen_sync_revision") or 0)
        filtered["last_job_id"] = str(filtered.get("last_job_id") or "")
        filtered["last_queue_at"] = str(filtered.get("last_queue_at") or "")
        return cls(**filtered)


@dataclass
class PlaylistSyncSummary:
    added: int = 0
    removed: int = 0
    became_unavailable: int = 0
    became_available: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Optional[dict]) -> "PlaylistSyncSummary":
        payload = payload or {}
        allowed = cls.__dataclass_fields__.keys()
        filtered = {k: int(payload.get(k) or 0) for k in allowed}
        return cls(**filtered)


@dataclass
class PlaylistRecord:
    playlist_id: str
    provider: str
    external_playlist_id: str
    source_url: str
    title: str = ""
    created_at: str = ""
    updated_at: str = ""
    last_sync_at: str = ""
    removed: bool = False
    sync_revision: int = 0
    last_sync_error: str = ""
    last_fetch_all_count: int = 0
    last_fetch_skipped_unavailable_count: int = 0
    last_fetch_mode: str = ""
    streams: list[PlaylistStreamRecord] = field(default_factory=list)
    last_sync_summary: PlaylistSyncSummary = field(default_factory=PlaylistSyncSummary)

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["streams"] = [stream.to_dict() for stream in self.streams]
        payload["last_sync_summary"] = self.last_sync_summary.to_dict()
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> "PlaylistRecord":
        streams = [
            PlaylistStreamRecord.from_dict(item)
            for item in (payload.get("streams") or [])
            if isinstance(item, dict)
        ]
        summary = PlaylistSyncSummary.from_dict(payload.get("last_sync_summary"))
        return cls(
            playlist_id=str(payload.get("playlist_id") or ""),
            provider=str(payload.get("provider") or "youtube"),
            external_playlist_id=str(payload.get("external_playlist_id") or ""),
            source_url=str(payload.get("source_url") or ""),
            title=str(payload.get("title") or ""),
            created_at=str(payload.get("created_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            last_sync_at=str(payload.get("last_sync_at") or ""),
            removed=bool(payload.get("removed") is True),
            sync_revision=int(payload.get("sync_revision") or 0),
            last_sync_error=str(payload.get("last_sync_error") or ""),
            last_fetch_all_count=int(payload.get("last_fetch_all_count") or 0),
            last_fetch_skipped_unavailable_count=int(payload.get("last_fetch_skipped_unavailable_count") or 0),
            last_fetch_mode=str(payload.get("last_fetch_mode") or ""),
            streams=streams,
            last_sync_summary=summary,
        )
