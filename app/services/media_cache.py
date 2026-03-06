import json
import urllib.request
from pathlib import Path
from typing import Optional

from app.core.config import settings


def _metadata_path(video_id: str) -> Path:
    return settings.metadata_cache_dir / f"{video_id}.json"


def load_cached_metadata(video_id: str) -> Optional[dict]:
    path = _metadata_path(video_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def save_cached_metadata(video_id: str, payload: dict) -> None:
    _metadata_path(video_id).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def cached_thumbnail_path(video_id: str) -> Path:
    return settings.thumbnail_cache_dir / f"{video_id}.jpg"


def has_cached_thumbnail(video_id: str) -> bool:
    return cached_thumbnail_path(video_id).exists()


def cache_thumbnail_from_url(video_id: str, thumbnail_url: str) -> Optional[Path]:
    if not thumbnail_url:
        return None

    target = cached_thumbnail_path(video_id)
    if target.exists():
        return target

    try:
        with urllib.request.urlopen(thumbnail_url, timeout=15) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            if "image" not in content_type:
                return None
            data = response.read()
    except Exception:
        return None

    if not data:
        return None

    target.write_bytes(data)
    return target if target.exists() else None
