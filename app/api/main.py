from pathlib import Path
import re
import time
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.core.config import settings
from app.core.models import STATUS_QUEUED, utc_now_iso
from app.providers.youtube import parse_youtube_ref, resolve_youtube_input_or_400, resolve_youtube_playlist_input_or_400
from app.services.job_store import list_completed_jobs, list_jobs, load_job, mark_job_removed
from app.services.playlist_store import list_playlists, load_playlist, mark_playlist_removed, save_playlist
from app.services.media_cache import load_cached_metadata, save_cached_metadata
from app.services.media_pipeline import PipelineError, fetch_video_metadata
from app.services.playlist_service import (
    build_job_lookup,
    build_playlist_views,
    count_new_streams,
    create_playlist_record,
    derive_stream_view,
    enqueue_job_for_url,
    queue_playlist_batch,
    queue_streams_from_playlist,
    sync_playlist_record,
)


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
def playlists_page(
    request: Request,
    notice: str = Query(""),
    new_count: int = Query(0),
    queued_count: int = Query(0),
    auto_select_new: int = Query(0),
    added_count: int = Query(0),
    removed_count: int = Query(0),
    unavailable_count: int = Query(0),
    available_count: int = Query(0),
):
    playlists = list_playlists()
    records = list_jobs(include_removed=False)
    playlist_items = build_playlist_views(playlists, records)
    return templates.TemplateResponse(
        "playlists.html",
        {
            **_base_context(request),
            "playlist_items": playlist_items,
            "notice": notice,
            "new_count": new_count,
            "queued_count": queued_count,
            "auto_select_new": auto_select_new == 1,
            "added_count": added_count,
            "removed_count": removed_count,
            "unavailable_count": unavailable_count,
            "available_count": available_count,
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
        synced, _ = sync_playlist_record(existing)
        if not synced:
            return RedirectResponse(url="/playlists?notice=sync_failed", status_code=303)
        summary = existing.get("last_sync_summary") or {}
        new_count = count_new_streams(existing)
        auto_select_new = 1 if new_count > 0 else 0
        return RedirectResponse(
            url=(
                "/playlists?notice=synced"
                f"&new_count={new_count}"
                f"&auto_select_new={auto_select_new}"
                f"&added_count={int(summary.get('added') or 0)}"
                f"&removed_count={int(summary.get('removed') or 0)}"
                f"&unavailable_count={int(summary.get('became_unavailable') or 0)}"
                f"&available_count={int(summary.get('became_available') or 0)}"
            ),
            status_code=303,
        )

    record = create_playlist_record(ref.playlist_id, ref.canonical_url)
    synced, _ = sync_playlist_record(record)
    if not synced:
        return RedirectResponse(url="/playlists?notice=sync_failed", status_code=303)
    summary = record.get("last_sync_summary") or {}
    new_count = count_new_streams(record)
    auto_select_new = 1 if new_count > 0 else 0
    return RedirectResponse(
        url=(
            "/playlists?notice=synced"
            f"&new_count={new_count}"
            f"&auto_select_new={auto_select_new}"
            f"&added_count={int(summary.get('added') or 0)}"
            f"&removed_count={int(summary.get('removed') or 0)}"
            f"&unavailable_count={int(summary.get('became_unavailable') or 0)}"
            f"&available_count={int(summary.get('became_available') or 0)}"
        ),
        status_code=303,
    )


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
        synced, error = sync_playlist_record(existing)
        if synced:
            title = existing.get("title") or "Playlist"
            count = len(existing.get("streams") or [])
            summary = existing.get("last_sync_summary") or {}
            message = (
                f"{title} synced ({count} streams; "
                f"{int(summary.get('added') or 0)} added, "
                f"{int(summary.get('removed') or 0)} removed, "
                f"{int(summary.get('became_unavailable') or 0)} unavailable, "
                f"{int(summary.get('became_available') or 0)} available again)."
            )
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

    record = create_playlist_record(ref.playlist_id, ref.canonical_url)
    synced, error = sync_playlist_record(record)
    if synced:
        title = record.get("title") or "Playlist"
        count = len(record.get("streams") or [])
        summary = record.get("last_sync_summary") or {}
        message = (
            f"{title} synced ({count} streams; "
            f"{int(summary.get('added') or 0)} added, "
            f"{int(summary.get('removed') or 0)} removed, "
            f"{int(summary.get('became_unavailable') or 0)} unavailable, "
            f"{int(summary.get('became_available') or 0)} available again)."
        )
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
    synced, error = sync_playlist_record(playlist)
    if not synced:
        raise HTTPException(status_code=400, detail=error)
    new_count = count_new_streams(playlist)
    summary = playlist.get("last_sync_summary") or {}
    auto_select_new = 1 if new_count > 0 else 0
    return RedirectResponse(
        url=(
            "/playlists?notice=synced"
            f"&new_count={new_count}"
            f"&auto_select_new={auto_select_new}"
            f"&added_count={int(summary.get('added') or 0)}"
            f"&removed_count={int(summary.get('removed') or 0)}"
            f"&unavailable_count={int(summary.get('became_unavailable') or 0)}"
            f"&available_count={int(summary.get('became_available') or 0)}"
        ),
        status_code=303,
    )


@app.post("/playlists/{playlist_id}/fetch-all")
def fetch_playlist_all(playlist_id: str, mode: str = Form("all")):
    playlist = load_playlist(playlist_id)
    if playlist is None or playlist.get("removed") is True:
        raise HTTPException(status_code=404, detail="Playlist not found")

    enqueued, skipped_unavailable = queue_playlist_batch(playlist, mode=mode)
    playlist["updated_at"] = utc_now_iso()
    playlist["last_fetch_all_count"] = enqueued
    playlist["last_fetch_skipped_unavailable_count"] = skipped_unavailable
    playlist["last_fetch_mode"] = mode
    save_playlist(playlist)
    return RedirectResponse(url=f"/playlists?notice=queued&queued_count={enqueued}", status_code=303)


@app.post("/playlists/{playlist_id}/queue-selected")
def queue_selected_playlist_streams(playlist_id: str, stream_ids: list[str] = Form(default=[])):
    playlist = load_playlist(playlist_id)
    if playlist is None or playlist.get("removed") is True:
        raise HTTPException(status_code=404, detail="Playlist not found")

    requested_ids = {str(stream_id or "").strip() for stream_id in stream_ids if str(stream_id or "").strip()}
    if not requested_ids:
        return RedirectResponse(url="/playlists?notice=no_selection", status_code=303)

    enqueued, _ = queue_streams_from_playlist(playlist, requested_ids)
    return RedirectResponse(url=f"/playlists?notice=queued&queued_count={enqueued}", status_code=303)


@app.post("/api/playlists/{playlist_id}/queue-stream")
def queue_single_playlist_stream_api(playlist_id: str, stream_id: str = Form(...)):
    playlist = load_playlist(playlist_id)
    if playlist is None or playlist.get("removed") is True:
        raise HTTPException(status_code=404, detail="Playlist not found")

    stream_id = str(stream_id or "").strip()
    if not stream_id:
        raise HTTPException(status_code=400, detail="stream_id is required")

    enqueued, queued_streams = queue_streams_from_playlist(playlist, {stream_id})
    if enqueued == 0:
        raise HTTPException(status_code=409, detail="Stream already queued, downloaded, unavailable, or missing")

    records = list_jobs(include_removed=False)
    completed_by_url, active_by_url, failed_by_url, latest_by_video_id = build_job_lookup(records)
    stream_view = derive_stream_view(
        queued_streams[0],
        playlist_sync_revision=int(playlist.get("sync_revision") or 0),
        completed_by_url=completed_by_url,
        active_by_url=active_by_url,
        failed_by_url=failed_by_url,
        latest_by_video_id=latest_by_video_id,
    )
    return {
        "ok": True,
        "message": f"Queued {stream_view['title']}.",
        "stream": stream_view,
    }


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
    record = enqueue_job_for_url(ref.canonical_url, ref.video_id)
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
