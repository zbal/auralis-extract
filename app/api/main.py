from pathlib import Path
import re
import time
from uuid import uuid4
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from rq import Retry

from app.core.config import settings
from app.core.models import JobRecord, STATUS_QUEUED, utc_now_iso
from app.core.queue import job_queue
from app.providers.youtube import resolve_youtube_input_or_400
from app.services.job_store import list_completed_jobs, list_jobs, load_job, mark_job_removed, save_job
from app.services.media_cache import load_cached_metadata, save_cached_metadata
from app.services.media_pipeline import PipelineError, fetch_video_metadata
from app.worker.tasks import process_job


app = FastAPI(title="auralis-extract")
templates = Jinja2Templates(directory="app/web/templates")
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
_preview_cache: dict[str, tuple[float, dict]] = {}
_preview_ttl_seconds = 600


class JobCreateRequest(BaseModel):
    url: str


def _base_context(request: Request) -> dict:
    return {"request": request, "github_url": settings.github_url}


def _title_similarity_key(title: str) -> str:
    lowered = title.lower()
    lowered = re.sub(r"\b(part|pt|episode|ep)\s*\d+\b", "", lowered)
    lowered = re.sub(r"[^a-z0-9\s]", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered).strip()
    return lowered


def _build_download_view(record: dict) -> dict:
    job_id = record.get("job_id")
    return {
        "job_id": job_id,
        "title": record.get("media_title") or "Unknown Title",
        "artist": record.get("media_artist") or "Unknown Artist",
        "thumbnail_url": record.get("media_thumbnail_url") or "",
        "source_url": record.get("input_url") or "",
        "status": record.get("status") or "unknown",
        "stage": record.get("stage") or "unknown",
        "progress": int(record.get("progress") or 0),
        "message": record.get("message") or "",
        "download_url": f"/download/{job_id}",
        "tracker_url": f"/?job_id={job_id}",
        "process_url": f"/?url={quote(record.get('input_url') or '')}&auto=1",
        "output_filename": record.get("output_filename") or "",
        "finished_at": record.get("finished_at") or record.get("created_at") or "",
    }


def _is_listable_job(record: dict) -> bool:
    return record.get("status") in {"queued", "running", "completed"}


def _filter_jobs(records: list[dict], query: str) -> list[dict]:
    q = (query or "").strip().lower()
    if not q:
        return records

    out: list[dict] = []
    for record in records:
        hay = " ".join(
            [
                str(record.get("media_title") or ""),
                str(record.get("media_artist") or ""),
                str(record.get("input_url") or ""),
                str(record.get("status") or ""),
            ]
        ).lower()
        if q in hay:
            out.append(record)
    return out


def _sort_jobs_newest_first(records: list[dict]) -> list[dict]:
    return sorted(
        records,
        key=lambda r: r.get("finished_at") or r.get("started_at") or r.get("created_at") or "",
        reverse=True,
    )


def _find_similar_downloads(artist: str, title: str, exclude_url: str = "") -> list[dict]:
    artist_key = (artist or "").strip().lower()
    title_key = _title_similarity_key(title or "")

    scored: list[tuple[int, dict]] = []
    for record in list_completed_jobs():
        source_url = (record.get("input_url") or "").strip()
        if exclude_url and source_url == exclude_url.strip():
            continue

        record_artist = (record.get("media_artist") or "").strip().lower()
        record_title_key = _title_similarity_key(record.get("media_title") or "")

        score = 0
        if artist_key and record_artist and artist_key == record_artist:
            score += 3

        if title_key and record_title_key:
            if title_key == record_title_key:
                score += 3
            elif title_key in record_title_key or record_title_key in title_key:
                score += 2
            elif len(title_key) >= 10 and len(record_title_key) >= 10:
                a_words = set(title_key.split())
                b_words = set(record_title_key.split())
                common = len(a_words.intersection(b_words))
                if common >= 3:
                    score += 1

        if score > 0:
            scored.append((score, record))

    scored.sort(key=lambda item: (item[0], item[1].get("finished_at") or ""), reverse=True)
    return [_build_download_view(record) for _, record in scored[:8]]


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    records = _sort_jobs_newest_first([r for r in list_jobs() if _is_listable_job(r)])
    job_items = [_build_download_view(r) for r in records]
    return templates.TemplateResponse("index.html", {**_base_context(request), "job_items": job_items})


@app.get("/downloads", response_class=HTMLResponse)
def downloads_page(
    request: Request,
):
    return RedirectResponse(url="/", status_code=307)


@app.get("/terms", response_class=HTMLResponse)
def terms_page(request: Request):
    return templates.TemplateResponse("terms.html", _base_context(request))


@app.get("/fair-usage", response_class=HTMLResponse)
def fair_usage_page(request: Request):
    return templates.TemplateResponse("fair-usage.html", _base_context(request))


@app.get("/preview")
def preview(url: str = Query(...)):
    ref = resolve_youtube_input_or_400(url)
    cached = _preview_cache.get(ref.video_id)
    now = time.monotonic()
    if cached and now - cached[0] <= _preview_ttl_seconds:
        return cached[1]

    disk_cached = load_cached_metadata(ref.video_id)
    if disk_cached:
        payload = {
            "url": ref.canonical_url,
            "video_id": ref.video_id,
            "title": disk_cached.get("title") or "Unknown Title",
            "artist": disk_cached.get("artist") or "Unknown Artist",
            "thumbnail_url": disk_cached.get("thumbnail_url") or "",
        }
        _preview_cache[ref.video_id] = (now, payload)
        return payload

    try:
        metadata = fetch_video_metadata(ref.canonical_url)
    except PipelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    payload = {
        "url": ref.canonical_url,
        "video_id": ref.video_id,
        "title": metadata.title,
        "artist": metadata.artist,
        "thumbnail_url": metadata.thumbnail_url,
    }
    save_cached_metadata(
        ref.video_id,
        {
            "title": metadata.title,
            "artist": metadata.artist,
            "thumbnail_url": metadata.thumbnail_url or "",
        },
    )
    _preview_cache[ref.video_id] = (now, payload)
    return payload


@app.get("/similar")
def similar(
    artist: str = Query(""),
    title: str = Query(""),
    exclude_url: str = Query(""),
):
    if not artist.strip() and not title.strip():
        return {"items": []}
    return {"items": _find_similar_downloads(artist=artist, title=title, exclude_url=exclude_url)}


@app.post("/jobs")
def create_job(payload: JobCreateRequest):
    ref = resolve_youtube_input_or_400(payload.url)

    job_id = str(uuid4())
    cached = load_cached_metadata(ref.video_id) or {}
    record = JobRecord(
        job_id=job_id,
        input_url=ref.canonical_url,
        status=STATUS_QUEUED,
        stage="queued",
        progress=0,
        created_at=utc_now_iso(),
        message="Queued",
        media_title=cached.get("title"),
        media_artist=cached.get("artist"),
        media_thumbnail_url=cached.get("thumbnail_url"),
    )
    save_job(record)

    job_queue.enqueue(
        process_job,
        job_id,
        ref.canonical_url,
        settings.target_lufs,
        job_id=job_id,
        retry=Retry(max=settings.max_job_retries, interval=[5, 15]),
        job_timeout=settings.job_timeout_seconds,
    )
    return {
        "job_id": job_id,
        "status": STATUS_QUEUED,
        "url": ref.canonical_url,
        "video_id": ref.video_id,
        "media_title": record.media_title,
        "media_artist": record.media_artist,
        "media_thumbnail_url": record.media_thumbnail_url,
    }


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    record = load_job(job_id)
    if record is None or record.get("removed") is True:
        raise HTTPException(status_code=404, detail="Job not found")
    return record


@app.delete("/jobs/{job_id}")
def remove_job(job_id: str):
    removed = mark_job_removed(job_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"job_id": job_id, "status": "removed"}


@app.get("/download/{job_id}")
def download(job_id: str):
    record = load_job(job_id)
    if record is None or record.get("removed") is True:
        raise HTTPException(status_code=404, detail="Job not found")
    if record.get("status") != "completed":
        raise HTTPException(status_code=400, detail="Job not completed")

    filename = record.get("output_filename")
    if not filename:
        raise HTTPException(status_code=500, detail="Missing output filename")

    file_path = settings.output_dir / filename
    if not Path(file_path).exists():
        raise HTTPException(status_code=404, detail="Output file missing")

    return FileResponse(file_path, media_type="audio/mpeg", filename=filename)
