import json
import os
from pathlib import Path
import time
from typing import Optional

from app.core.config import settings
from app.core.models import JobRecord


def _job_path(job_id: str) -> Path:
    return settings.jobs_dir / f"{job_id}.json"


def save_job(record: JobRecord) -> None:
    _write_json_atomic(_job_path(record.job_id), record.to_dict())


def load_job(job_id: str) -> Optional[dict]:
    path = _job_path(job_id)
    if not path.exists():
        return None
    # Worker and API may touch the same file concurrently; retry short transient reads.
    for attempt in range(3):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            if attempt == 2:
                return None
            time.sleep(0.03)


def list_jobs(include_removed: bool = False) -> list[dict]:
    records: list[dict] = []
    for path in settings.jobs_dir.glob("*.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not include_removed and raw.get("removed") is True:
                continue
            records.append(raw)
        except json.JSONDecodeError:
            continue
    return records


def list_completed_jobs() -> list[dict]:
    records = [r for r in list_jobs() if r.get("status") == "completed" and r.get("output_filename")]
    records.sort(key=lambda r: r.get("finished_at") or r.get("created_at") or "", reverse=True)
    return records


def mark_job_removed(job_id: str) -> bool:
    record = load_job(job_id)
    if record is None:
        return False
    record["removed"] = True
    _write_json_atomic(_job_path(job_id), record)
    return True


def _write_json_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, path)
