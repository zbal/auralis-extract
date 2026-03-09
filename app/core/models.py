from dataclasses import dataclass, asdict
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
