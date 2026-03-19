import json
import os
from pathlib import Path
import time
from typing import Optional

from app.core.models import PlaylistRecord
from app.core.config import settings


def _playlist_path(playlist_id: str) -> Path:
    return settings.playlists_dir / f"{playlist_id}.json"


def save_playlist(record: dict | PlaylistRecord) -> None:
    payload = record.to_dict() if isinstance(record, PlaylistRecord) else record
    playlist_id = str(payload.get("playlist_id") or "").strip()
    if not playlist_id:
        raise ValueError("playlist_id is required")
    normalized = PlaylistRecord.from_dict(payload).to_dict()
    _write_json_atomic(_playlist_path(playlist_id), normalized)


def load_playlist(playlist_id: str) -> Optional[dict]:
    path = _playlist_path(playlist_id)
    if not path.exists():
        return None
    for attempt in range(3):
        try:
            return PlaylistRecord.from_dict(json.loads(path.read_text(encoding="utf-8"))).to_dict()
        except json.JSONDecodeError:
            if attempt == 2:
                return None
            time.sleep(0.03)


def list_playlists(include_removed: bool = False) -> list[dict]:
    records: list[dict] = []
    for path in settings.playlists_dir.glob("*.json"):
        try:
            raw = PlaylistRecord.from_dict(json.loads(path.read_text(encoding="utf-8"))).to_dict()
            if not include_removed and raw.get("removed") is True:
                continue
            records.append(raw)
        except json.JSONDecodeError:
            continue
    return records


def mark_playlist_removed(playlist_id: str) -> bool:
    record = load_playlist(playlist_id)
    if record is None:
        return False
    record["removed"] = True
    record["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write_json_atomic(_playlist_path(playlist_id), record)
    return True


def _write_json_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)
