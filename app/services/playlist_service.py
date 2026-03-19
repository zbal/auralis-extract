from datetime import datetime, timezone
from uuid import uuid4

from app.core.config import settings
from app.core.models import (
    JobRecord,
    PlaylistRecord,
    PlaylistStreamRecord,
    PlaylistSyncSummary,
    STATUS_QUEUED,
    utc_now_iso,
)
from app.providers.youtube import parse_youtube_ref
from app.services.job_store import list_jobs, save_job
from app.services.media_cache import load_cached_metadata
from app.services.media_pipeline import PipelineError, fetch_playlist_metadata
from app.services.playlist_store import save_playlist


def create_playlist_record(external_playlist_id: str, source_url: str) -> dict:
    now = utc_now_iso()
    return PlaylistRecord(
        playlist_id=str(uuid4()),
        provider="youtube",
        external_playlist_id=external_playlist_id,
        source_url=source_url,
        created_at=now,
        updated_at=now,
    ).to_dict()


def enqueue_job_for_url(
    canonical_url: str,
    video_id: str,
    media_title: str | None = None,
    media_artist: str | None = None,
    media_thumbnail_url: str | None = None,
) -> dict:
    from rq import Retry

    from app.core.queue import job_queue
    from app.worker.tasks import process_job

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


def sync_playlist_record(playlist: dict) -> tuple[bool, str]:
    original_playlist = playlist
    playlist_record = PlaylistRecord.from_dict(playlist)
    playlist = playlist_record.to_dict()
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

    previous_streams = [
        stream
        for stream in (playlist.get("streams") or [])
        if isinstance(stream, dict)
    ]
    previous_revision = int(playlist.get("sync_revision") or 0)
    next_revision = previous_revision + 1
    had_prior_snapshot = bool(playlist.get("last_sync_at")) or bool(playlist.get("streams"))
    playlist["title"] = payload.get("title") or playlist.get("title") or "Untitled Playlist"
    next_streams = extract_playlist_streams(
        payload,
        existing_playlist=playlist,
        current_sync_revision=next_revision,
        treat_existing_items_as_old=had_prior_snapshot,
    )
    playlist["streams"] = next_streams
    playlist["sync_revision"] = next_revision
    playlist["last_sync_at"] = utc_now_iso()
    playlist["updated_at"] = utc_now_iso()
    playlist["last_sync_error"] = ""
    playlist["last_sync_summary"] = summarize_sync_changes(previous_streams, next_streams)
    normalized = PlaylistRecord.from_dict(playlist).to_dict()
    playlist.clear()
    playlist.update(normalized)
    original_playlist.clear()
    original_playlist.update(playlist)
    save_playlist(original_playlist)
    return (True, "")


def extract_playlist_streams(
    playlist_payload: dict,
    existing_playlist: dict | None = None,
    current_sync_revision: int = 0,
    treat_existing_items_as_old: bool = False,
) -> list[dict]:
    existing_playlist_record = PlaylistRecord.from_dict(existing_playlist or {}) if existing_playlist else None
    entries = playlist_payload.get("entries")
    if not isinstance(entries, list):
        return []

    existing_streams_by_id: dict[str, dict] = {}
    if existing_playlist_record:
        for stream in existing_playlist_record.streams:
            if stream.video_id:
                existing_streams_by_id[stream.video_id] = stream.to_dict()

    streams: list[dict] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        availability = str(item.get("availability") or "").strip().lower()
        status, reason = classify_playlist_stream_state(title=title, availability=availability)

        video_id = str(item.get("id") or "").strip()
        if not video_id:
            continue
        url = f"https://www.youtube.com/watch?v={video_id}"
        existing = existing_streams_by_id.get(video_id) or {}
        if existing.get("discovered_sync_revision") is not None:
            discovered_sync_revision = int(existing.get("discovered_sync_revision") or 0)
        elif existing and treat_existing_items_as_old:
            discovered_sync_revision = 0
        else:
            discovered_sync_revision = current_sync_revision
        streams.append(
            PlaylistStreamRecord(
                video_id=video_id,
                url=url,
                title=title or "Unknown Title",
                artist=str(item.get("uploader") or item.get("channel") or "Unknown Artist"),
                thumbnail_url=str(item.get("thumbnail") or ""),
                position=item.get("playlist_index"),
                availability=availability,
                availability_status=status,
                availability_reason=reason,
                availability_last_checked_at=utc_now_iso(),
                discovered_sync_revision=discovered_sync_revision,
                last_seen_sync_revision=current_sync_revision,
                last_job_id=str(existing.get("last_job_id") or ""),
                last_queue_at=str(existing.get("last_queue_at") or ""),
            ).to_dict()
        )
    return streams


def classify_playlist_stream_state(
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


def refresh_stream_eligibility(stream: dict) -> None:
    status, reason = classify_playlist_stream_state(
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

    cached = load_cached_metadata(video_id) or {}
    if cached:
        stream["title"] = cached.get("title") or stream.get("title") or "Unknown Title"
        stream["artist"] = cached.get("artist") or stream.get("artist") or "Unknown Artist"
        stream["thumbnail_url"] = cached.get("thumbnail_url") or stream.get("thumbnail_url") or ""

    stream["availability_status"] = "available"
    stream["availability_reason"] = ""


def parse_iso_for_sort(value: str | None) -> datetime:
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


def format_human_day_time(value: str | None) -> str:
    parsed = parse_iso_for_sort(value)
    if parsed.year == datetime.min.year:
        return "Never"
    return parsed.astimezone(timezone.utc).strftime("%d %b %H:%M UTC")


def stream_state_label(state: str) -> str:
    labels = {
        "downloaded": "Downloaded",
        "queued": "Queued",
        "failed": "Failed",
        "new": "New",
        "unavailable": "Unavailable",
    }
    return labels.get(state, state.replace("_", " ").title())


def build_job_lookup(records: list[dict]) -> tuple[dict[str, dict], dict[str, dict], dict[str, dict], dict[str, dict]]:
    completed_by_url: dict[str, dict] = {}
    active_by_url: dict[str, dict] = {}
    failed_by_url: dict[str, dict] = {}
    latest_by_video_id: dict[str, dict] = {}

    def remember(target: dict[str, dict], key: str, record: dict, timestamp: str) -> None:
        if not key:
            return
        existing = target.get(key)
        if existing is None or parse_iso_for_sort(timestamp) > parse_iso_for_sort(
            existing.get("finished_at") or existing.get("started_at") or existing.get("created_at")
        ):
            target[key] = record

    for record in records:
        input_url = str(record.get("input_url") or "").strip()
        video_id = str(record.get("source_video_id") or "").strip()
        status = str(record.get("status") or "").strip()
        timestamp = record.get("finished_at") or record.get("started_at") or record.get("created_at") or ""

        if status == "completed":
            remember(completed_by_url, input_url, record, timestamp)
        elif status in {"queued", "running"}:
            remember(active_by_url, input_url, record, timestamp)
        elif status == "failed":
            remember(failed_by_url, input_url, record, timestamp)

        remember(latest_by_video_id, video_id, record, timestamp)
        if not video_id and input_url:
            ref = parse_youtube_ref(input_url)
            if ref:
                remember(latest_by_video_id, ref.video_id, record, timestamp)

    return completed_by_url, active_by_url, failed_by_url, latest_by_video_id


def derive_stream_view(
    stream: dict,
    playlist_sync_revision: int,
    completed_by_url: dict[str, dict],
    active_by_url: dict[str, dict],
    failed_by_url: dict[str, dict],
    latest_by_video_id: dict[str, dict],
) -> dict:
    stream_url = str(stream.get("url") or "").strip()
    video_id = str(stream.get("video_id") or "").strip()
    completed_job = completed_by_url.get(stream_url)
    active_job = active_by_url.get(stream_url)
    failed_job = failed_by_url.get(stream_url)
    latest_job = latest_by_video_id.get(video_id) or {}
    availability_status = str(stream.get("availability_status") or "available").strip()
    discovered_sync_revision = int(stream.get("discovered_sync_revision") or 0)
    is_new = discovered_sync_revision == playlist_sync_revision

    state = "undownloaded"
    badge_tone = "muted"
    helper_text = ""
    selectable = True
    download_url = ""

    if completed_job:
        state = "downloaded"
        badge_tone = "success"
        selectable = False
        download_url = f"/download/{completed_job.get('job_id')}"
    elif active_job:
        state = "queued"
        badge_tone = "info"
        helper_text = "Already queued"
        selectable = False
    elif failed_job:
        state = "failed"
        badge_tone = "danger"
        helper_text = str(failed_job.get("message") or "Last attempt failed")
    elif availability_status != "available":
        state = "unavailable"
        helper_text = str(stream.get("availability_reason") or "Unavailable").replace("_", " ")
        selectable = False
    elif is_new:
        state = "new"
        badge_tone = "new"

    return {
        "video_id": video_id,
        "url": stream_url,
        "title": str(stream.get("title") or latest_job.get("media_title") or "Unknown Title"),
        "artist": str(stream.get("artist") or latest_job.get("media_artist") or "Unknown Artist"),
        "thumbnail_url": str(stream.get("thumbnail_url") or latest_job.get("media_thumbnail_url") or ""),
        "position": stream.get("position"),
        "state": state,
        "state_label": stream_state_label(state),
        "badge_tone": badge_tone,
        "helper_text": helper_text,
        "is_new": is_new,
        "selectable": selectable,
        "download_url": download_url,
    }


def build_playlist_views(playlists: list[dict], records: list[dict]) -> list[dict]:
    completed_by_url, active_by_url, failed_by_url, latest_by_video_id = build_job_lookup(records)

    views: list[dict] = []
    for playlist in playlists:
        streams = playlist.get("streams")
        if not isinstance(streams, list):
            streams = []
        playlist_sync_revision = int(playlist.get("sync_revision") or 0)
        total_streams = len(streams)
        downloaded_count = 0
        new_count = 0
        queued_count = 0
        failed_count = 0
        stream_items: list[dict] = []

        for stream in streams:
            if not isinstance(stream, dict):
                continue
            stream_view = derive_stream_view(
                stream,
                playlist_sync_revision=playlist_sync_revision,
                completed_by_url=completed_by_url,
                active_by_url=active_by_url,
                failed_by_url=failed_by_url,
                latest_by_video_id=latest_by_video_id,
            )
            stream_items.append(stream_view)

            if stream_view["state"] == "downloaded":
                downloaded_count += 1
            elif stream_view["state"] == "queued":
                queued_count += 1
            elif stream_view["state"] == "failed":
                failed_count += 1

            if stream_view["is_new"]:
                new_count += 1

        stream_items.sort(key=lambda item: (item.get("position") is None, item.get("position") or 0))

        views.append(
            {
                "playlist_id": playlist.get("playlist_id"),
                "title": playlist.get("title") or "Untitled Playlist",
                "source_url": playlist.get("source_url") or "",
                "last_sync_at": playlist.get("last_sync_at") or "",
                "last_sync_label": format_human_day_time(playlist.get("last_sync_at")),
                "total_streams": total_streams,
                "downloaded_count": downloaded_count,
                "new_count": new_count,
                "queued_count": queued_count,
                "failed_count": failed_count,
                "stream_items": stream_items,
            }
        )

    views.sort(key=lambda p: parse_iso_for_sort(p.get("last_sync_at")), reverse=True)
    return views


def collect_existing_queue_targets(records: list[dict]) -> tuple[set[str], set[str]]:
    existing_video_ids: set[str] = set()
    existing_urls: set[str] = set()
    for record in records:
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
    return existing_video_ids, existing_urls


def queue_streams_from_playlist(playlist: dict, requested_ids: set[str]) -> tuple[int, list[dict]]:
    streams = playlist.get("streams")
    if not isinstance(streams, list):
        streams = []

    existing_records = list_jobs(include_removed=False)
    existing_video_ids, existing_urls = collect_existing_queue_targets(existing_records)

    enqueued = 0
    queued_streams: list[dict] = []
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        video_id = str(stream.get("video_id") or "").strip()
        canonical_url = str(stream.get("url") or "").strip()
        if video_id not in requested_ids or not canonical_url:
            continue

        refresh_stream_eligibility(stream)
        if str(stream.get("availability_status") or "") != "available":
            continue
        if video_id in existing_video_ids or canonical_url in existing_urls:
            continue

        job = enqueue_job_for_url(
            canonical_url=canonical_url,
            video_id=video_id,
            media_title=str(stream.get("title") or "Unknown Title"),
            media_artist=str(stream.get("artist") or "Unknown Artist"),
            media_thumbnail_url=str(stream.get("thumbnail_url") or ""),
        )
        stream["last_job_id"] = job.get("job_id") or ""
        stream["last_queue_at"] = utc_now_iso()
        existing_video_ids.add(video_id)
        existing_urls.add(canonical_url)
        enqueued += 1
        queued_streams.append(stream)

    playlist["updated_at"] = utc_now_iso()
    save_playlist(playlist)
    return enqueued, queued_streams


def queue_playlist_batch(playlist: dict, mode: str = "all") -> tuple[int, int]:
    streams = playlist.get("streams")
    if not isinstance(streams, list):
        streams = []

    selected_streams = streams
    if mode == "newest10":
        selected_streams = streams[:10]
    elif mode == "new":
        current_revision = int(playlist.get("sync_revision") or 0)
        selected_streams = [
            stream
            for stream in streams
            if isinstance(stream, dict) and int(stream.get("discovered_sync_revision") or 0) == current_revision
        ]

    requested_ids = {
        str(stream.get("video_id") or "").strip()
        for stream in selected_streams
        if isinstance(stream, dict) and str(stream.get("video_id") or "").strip()
    }
    unavailable_ids: set[str] = set()
    for stream in selected_streams:
        if not isinstance(stream, dict):
            continue
        refresh_stream_eligibility(stream)
        if str(stream.get("availability_status") or "") != "available":
            video_id = str(stream.get("video_id") or "").strip()
            if video_id:
                unavailable_ids.add(video_id)

    enqueued, _ = queue_streams_from_playlist(playlist, requested_ids)
    return enqueued, len(unavailable_ids)


def count_new_streams(playlist: dict) -> int:
    current_revision = int(playlist.get("sync_revision") or 0)
    total = 0
    for stream in playlist.get("streams") or []:
        if not isinstance(stream, dict):
            continue
        if int(stream.get("discovered_sync_revision") or 0) == current_revision:
            total += 1
    return total


def summarize_sync_changes(previous_streams: list[dict], next_streams: list[dict]) -> dict:
    previous_by_id = {
        str(stream.get("video_id") or "").strip(): stream
        for stream in previous_streams
        if str(stream.get("video_id") or "").strip()
    }
    next_by_id = {
        str(stream.get("video_id") or "").strip(): stream
        for stream in next_streams
        if str(stream.get("video_id") or "").strip()
    }

    previous_ids = set(previous_by_id)
    next_ids = set(next_by_id)

    added = len(next_ids - previous_ids)
    removed = len(previous_ids - next_ids)
    became_unavailable = 0
    became_available = 0

    for video_id in previous_ids.intersection(next_ids):
        before = str(previous_by_id[video_id].get("availability_status") or "available")
        after = str(next_by_id[video_id].get("availability_status") or "available")
        before_available = before == "available"
        after_available = after == "available"
        if before_available and not after_available:
            became_unavailable += 1
        elif not before_available and after_available:
            became_available += 1

    return PlaylistSyncSummary(
        added=added,
        removed=removed,
        became_unavailable=became_unavailable,
        became_available=became_available,
    ).to_dict()
