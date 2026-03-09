from datetime import datetime, timezone
from pathlib import Path
import re
import time
from uuid import uuid4
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from rq import Retry

from app.core.config import settings
from app.core.models import JobRecord, STATUS_QUEUED, utc_now_iso
from app.core.queue import job_queue
from app.providers.youtube import parse_youtube_ref, resolve_youtube_input_or_400, resolve_youtube_playlist_input_or_400
from app.services.job_store import list_completed_jobs, list_jobs, load_job, mark_job_removed, save_job
from app.services.playlist_store import list_playlists, load_playlist, mark_playlist_removed, save_playlist
from app.services.media_cache import load_cached_metadata, save_cached_metadata
from app.services.media_pipeline import PipelineError, fetch_playlist_metadata, fetch_video_metadata
from app.worker.tasks import process_job


app = FastAPI(title="auralis-extract")
templates = Jinja2Templates(directory="app/web/templates")
app.mount("/static", StaticFiles(directory="app/web/static"), name="static")
_preview_cache: dict[str, tuple[float, dict]] = {}
_preview_ttl_seconds = 600


class JobCreateRequest(BaseModel):
    url: str


class PlaylistCreateRequest(BaseModel):
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


def _sync_playlist_record(playlist: dict) -> tuple[bool, str]:
    source_url = str(playlist.get("source_url") or "").strip()
    if not source_url:
        playlist["updated_at"] = utc_now_iso()
        playlist["last_sync_error"] = "Playlist source URL is missing"
        save_playlist(playlist)
        return (False, "Playlist source URL is missing")

    try:
        payload = fetch_playlist_metadata(source_url)
    except PipelineError as exc:
        playlist["updated_at"] = utc_now_iso()
        playlist["last_sync_error"] = str(exc)
        save_playlist(playlist)
        return (False, str(exc))

    playlist["title"] = payload.get("title") or playlist.get("title") or "Untitled Playlist"
    playlist["streams"] = _extract_playlist_streams(payload)
    playlist["last_sync_at"] = utc_now_iso()
    playlist["updated_at"] = utc_now_iso()
    playlist["last_sync_error"] = ""
    save_playlist(playlist)
    return (True, "")


def _enqueue_job_for_url(
    canonical_url: str,
    video_id: str,
    media_title: str | None = None,
    media_artist: str | None = None,
    media_thumbnail_url: str | None = None,
) -> dict:
    job_id = str(uuid4())
    cached = load_cached_metadata(video_id) or {}
    record = JobRecord(
        job_id=job_id,
        input_url=canonical_url,
        source_video_id=video_id,
        status=STATUS_QUEUED,
        stage="queued",
        progress=0,
        created_at=utc_now_iso(),
        message="Queued",
        media_title=media_title or cached.get("title"),
        media_artist=media_artist or cached.get("artist"),
        media_thumbnail_url=media_thumbnail_url or cached.get("thumbnail_url"),
    )
    save_job(record)

    job_queue.enqueue(
        process_job,
        job_id,
        canonical_url,
        settings.target_lufs,
        job_id=job_id,
        retry=Retry(max=settings.max_job_retries, interval=[5, 15]),
        job_timeout=settings.job_timeout_seconds,
    )
    return record.to_dict()


def _extract_playlist_streams(playlist_payload: dict) -> list[dict]:
    entries = playlist_payload.get("entries")
    if not isinstance(entries, list):
        return []

    streams: list[dict] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        availability = str(item.get("availability") or "").strip().lower()
        status, reason = _classify_playlist_stream_state(title=title, availability=availability)

        video_id = str(item.get("id") or "").strip()
        if not video_id:
            continue
        url = f"https://www.youtube.com/watch?v={video_id}"
        streams.append(
            {
                "video_id": video_id,
                "url": url,
                "title": title or "Unknown Title",
                "artist": str(item.get("uploader") or item.get("channel") or "Unknown Artist"),
                "thumbnail_url": str(item.get("thumbnail") or ""),
                "position": item.get("playlist_index"),
                "availability": availability,
                "availability_status": status,
                "availability_reason": reason,
                "availability_last_checked_at": utc_now_iso(),
            }
        )
    return streams


def _classify_playlist_stream_state(
    title: str | None,
    availability: str | None,
    error_text: str | None = None,
) -> tuple[str, str]:
    title_key = str(title or "").strip().lower()
    availability_key = str(availability or "").strip().lower()
    err = str(error_text or "").strip().lower()

    if title_key in {"[deleted video]", "deleted video"} or availability_key == "deleted":
        return ("permanently_unavailable", "deleted")

    if (
        title_key in {"[private video]", "private video"}
        or availability_key in {"private", "needs_auth", "subscriber_only", "premium_only"}
    ):
        return ("temporarily_unavailable", "private_or_auth_required")

    if any(token in err for token in ["private video", "sign in", "cookies", "members-only", "age-restricted"]):
        return ("temporarily_unavailable", "auth_required_or_private")
    if any(token in err for token in ["video unavailable", "this live event will begin", "premiere", "not yet available"]):
        return ("temporarily_unavailable", "not_yet_available")
    if any(token in err for token in ["has been removed", "copyright claim", "terminated"]):
        return ("permanently_unavailable", "removed_or_blocked")

    return ("available", "")


def _is_unavailable_playlist_stream(title: str | None, availability: str | None) -> bool:
    status, _ = _classify_playlist_stream_state(title=title, availability=availability)
    return status != "available"


def _refresh_stream_eligibility(stream: dict) -> None:
    status, reason = _classify_playlist_stream_state(
        title=str(stream.get("title") or ""),
        availability=str(stream.get("availability") or ""),
    )
    stream["availability_status"] = status
    stream["availability_reason"] = reason
    stream["availability_last_checked_at"] = utc_now_iso()

    if status != "available":
        return

    canonical_url = str(stream.get("url") or "").strip()
    video_id = str(stream.get("video_id") or "").strip()
    if not canonical_url or not video_id:
        stream["availability_status"] = "temporarily_unavailable"
        stream["availability_reason"] = "invalid_stream_reference"
        return

    try:
        metadata = fetch_video_metadata(canonical_url)
        stream["title"] = metadata.title or stream.get("title") or "Unknown Title"
        stream["artist"] = metadata.artist or stream.get("artist") or "Unknown Artist"
        stream["thumbnail_url"] = metadata.thumbnail_url or stream.get("thumbnail_url") or ""
        save_cached_metadata(
            video_id,
            {
                "title": stream["title"],
                "artist": stream["artist"],
                "thumbnail_url": stream["thumbnail_url"],
            },
        )
        stream["availability_status"] = "available"
        stream["availability_reason"] = ""
    except PipelineError as exc:
        checked_status, checked_reason = _classify_playlist_stream_state(
            title=str(stream.get("title") or ""),
            availability=str(stream.get("availability") or ""),
            error_text=str(exc),
        )
        stream["availability_status"] = checked_status
        stream["availability_reason"] = checked_reason


def _parse_iso_for_sort(value: str | None) -> datetime:
    raw = (value or "").strip()
    if not raw:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _format_human_day_time(value: str | None) -> str:
    parsed = _parse_iso_for_sort(value)
    if parsed.year == datetime.min.year:
        return "Never"
    return parsed.astimezone(timezone.utc).strftime("%d %b %H:%M UTC")


def _build_playlist_views(playlists: list[dict], records: list[dict]) -> list[dict]:
    job_by_url: dict[str, dict] = {}
    for record in records:
        if record.get("status") != "completed":
            continue
        input_url = str(record.get("input_url") or "").strip()
        if not input_url:
            continue
        existing = job_by_url.get(input_url)
        if existing is None:
            job_by_url[input_url] = record
            continue
        if _parse_iso_for_sort(record.get("finished_at") or record.get("created_at")) > _parse_iso_for_sort(
            existing.get("finished_at") or existing.get("created_at")
        ):
            job_by_url[input_url] = record

    views: list[dict] = []
    for playlist in playlists:
        streams = playlist.get("streams")
        if not isinstance(streams, list):
            streams = []
        total_streams = len(streams)
        fetched_streams = 0
        downloaded_items: list[dict] = []

        for stream in streams:
            if not isinstance(stream, dict):
                continue
            stream_url = str(stream.get("url") or "").strip()
            completed_job = job_by_url.get(stream_url)
            if not completed_job:
                continue
            fetched_streams += 1
            downloaded_items.append(
                {
                    "title": completed_job.get("media_title") or stream.get("title") or "Unknown Title",
                    "artist": completed_job.get("media_artist") or stream.get("artist") or "Unknown Artist",
                    "thumbnail_url": completed_job.get("media_thumbnail_url") or stream.get("thumbnail_url") or "",
                    "download_url": f"/download/{completed_job.get('job_id')}",
                }
            )

        views.append(
            {
                "playlist_id": playlist.get("playlist_id"),
                "title": playlist.get("title") or "Untitled Playlist",
                "source_url": playlist.get("source_url") or "",
                "last_sync_at": playlist.get("last_sync_at") or "",
                "last_sync_label": _format_human_day_time(playlist.get("last_sync_at")),
                "total_streams": total_streams,
                "fetched_streams": fetched_streams,
                "downloaded_items": downloaded_items,
            }
        )

    views.sort(
        key=lambda p: _parse_iso_for_sort(p.get("last_sync_at")),
        reverse=True,
    )
    return views


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


@app.get("/playlists", response_class=HTMLResponse)
def playlists_page(request: Request, notice: str = Query("")):
    playlists = list_playlists()
    records = list_jobs(include_removed=False)
    playlist_items = _build_playlist_views(playlists, records)
    return templates.TemplateResponse(
        "playlists.html",
        {
            **_base_context(request),
            "playlist_items": playlist_items,
            "notice": notice,
        },
    )


@app.post("/playlists")
def create_playlist(url: str = Form(...)):
    ref = resolve_youtube_playlist_input_or_400(url)
    existing = next(
        (p for p in list_playlists(include_removed=True) if p.get("external_playlist_id") == ref.playlist_id),
        None,
    )
    if existing:
        existing["removed"] = False
        existing["source_url"] = ref.canonical_url
        synced, _ = _sync_playlist_record(existing)
        return RedirectResponse(url=f"/playlists?notice={'synced' if synced else 'sync_failed'}", status_code=303)

    now = utc_now_iso()
    record = {
        "playlist_id": str(uuid4()),
        "provider": "youtube",
        "external_playlist_id": ref.playlist_id,
        "source_url": ref.canonical_url,
        "title": "",
        "created_at": now,
        "updated_at": now,
        "last_sync_at": "",
        "removed": False,
        "streams": [],
    }
    synced, _ = _sync_playlist_record(record)
    return RedirectResponse(url=f"/playlists?notice={'synced' if synced else 'sync_failed'}", status_code=303)


@app.post("/playlists/intake")
def intake_playlist(payload: PlaylistCreateRequest):
    ref = resolve_youtube_playlist_input_or_400(payload.url)
    existing = next(
        (p for p in list_playlists(include_removed=True) if p.get("external_playlist_id") == ref.playlist_id),
        None,
    )
    if existing:
        was_removed = existing.get("removed") is True
        existing["removed"] = False
        existing["source_url"] = ref.canonical_url
        synced, error = _sync_playlist_record(existing)
        if synced:
            title = existing.get("title") or "Playlist"
            count = len(existing.get("streams") or [])
            message = f"{title} synced ({count} streams)."
        else:
            message = f"Playlist updated but sync failed: {error}"
        return {
            "playlist_id": existing.get("playlist_id"),
            "created": False,
            "restored": was_removed,
            "status": "updated",
            "message": message,
            "synced": synced,
        }

    now = utc_now_iso()
    record = {
        "playlist_id": str(uuid4()),
        "provider": "youtube",
        "external_playlist_id": ref.playlist_id,
        "source_url": ref.canonical_url,
        "title": "",
        "created_at": now,
        "updated_at": now,
        "last_sync_at": "",
        "removed": False,
        "streams": [],
    }
    synced, error = _sync_playlist_record(record)
    if synced:
        title = record.get("title") or "Playlist"
        count = len(record.get("streams") or [])
        message = f"{title} synced ({count} streams)."
    else:
        message = f"Playlist added but sync failed: {error}"
    return {
        "playlist_id": record["playlist_id"],
        "created": True,
        "restored": False,
        "status": "added",
        "message": message,
        "synced": synced,
    }


@app.post("/playlists/{playlist_id}/sync")
def sync_playlist(playlist_id: str):
    playlist = load_playlist(playlist_id)
    if playlist is None or playlist.get("removed") is True:
        raise HTTPException(status_code=404, detail="Playlist not found")
    synced, error = _sync_playlist_record(playlist)
    if not synced:
        raise HTTPException(status_code=400, detail=error)
    return RedirectResponse(url="/playlists?notice=synced", status_code=303)


@app.post("/playlists/{playlist_id}/fetch-all")
def fetch_playlist_all(playlist_id: str, mode: str = Form("all")):
    playlist = load_playlist(playlist_id)
    if playlist is None or playlist.get("removed") is True:
        raise HTTPException(status_code=404, detail="Playlist not found")

    streams = playlist.get("streams")
    if not isinstance(streams, list):
        streams = []

    selected_streams = streams
    if mode == "newest10":
        selected_streams = streams[:10]

    existing_records = list_jobs(include_removed=False)
    existing_video_ids: set[str] = set()
    existing_urls: set[str] = set()
    for record in existing_records:
        if record.get("status") not in {"queued", "running", "completed"}:
            continue
        raw_video_id = str(record.get("source_video_id") or "").strip()
        if raw_video_id:
            existing_video_ids.add(raw_video_id)
        input_url = str(record.get("input_url") or "").strip()
        if input_url:
            existing_urls.add(input_url)
        ref = parse_youtube_ref(input_url)
        if ref:
            existing_video_ids.add(ref.video_id)
            existing_urls.add(ref.canonical_url)

    enqueued = 0
    skipped_unavailable = 0
    for stream in selected_streams:
        if not isinstance(stream, dict):
            continue
        _refresh_stream_eligibility(stream)
        if str(stream.get("availability_status") or "") != "available":
            skipped_unavailable += 1
            continue
        canonical_url = str(stream.get("url") or "").strip()
        video_id = str(stream.get("video_id") or "").strip()
        if not canonical_url or not video_id:
            continue
        if video_id in existing_video_ids or canonical_url in existing_urls:
            continue

        _enqueue_job_for_url(
            canonical_url=canonical_url,
            video_id=video_id,
            media_title=str(stream.get("title") or "Unknown Title"),
            media_artist=str(stream.get("artist") or "Unknown Artist"),
            media_thumbnail_url=str(stream.get("thumbnail_url") or ""),
        )
        existing_video_ids.add(video_id)
        existing_urls.add(canonical_url)
        enqueued += 1

    playlist["updated_at"] = utc_now_iso()
    playlist["last_fetch_all_count"] = enqueued
    playlist["last_fetch_skipped_unavailable_count"] = skipped_unavailable
    playlist["last_fetch_mode"] = mode
    save_playlist(playlist)
    return RedirectResponse(url="/playlists", status_code=303)


@app.post("/playlists/{playlist_id}/delete")
def delete_playlist(playlist_id: str):
    removed = mark_playlist_removed(playlist_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return RedirectResponse(url="/playlists", status_code=303)


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
    record = _enqueue_job_for_url(ref.canonical_url, ref.video_id)
    return {
        "job_id": record["job_id"],
        "status": STATUS_QUEUED,
        "url": ref.canonical_url,
        "video_id": ref.video_id,
        "media_title": record.get("media_title"),
        "media_artist": record.get("media_artist"),
        "media_thumbnail_url": record.get("media_thumbnail_url"),
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
