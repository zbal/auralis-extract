import shutil
import time

from app.core.config import settings
from app.core.models import (
    JobRecord,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_RUNNING,
    utc_now_iso,
)
from app.providers.youtube import parse_youtube_ref
from app.services.media_cache import (
    cache_thumbnail_from_url,
    cached_thumbnail_path,
    has_cached_thumbnail,
    load_cached_metadata,
    save_cached_metadata,
)
from app.services.job_store import load_job, save_job
from app.services.media_pipeline import (
    build_output_filename,
    ensure_unique_output_path,
    fetch_video_metadata,
    PipelineError,
    download_audio_source,
    job_work_dir,
    normalize_to_mp3_128k,
)


def process_job(job_id: str, url: str, target_i: int = -16) -> None:
    raw = load_job(job_id)
    if raw is None:
        record = JobRecord(job_id=job_id, input_url=url, status=STATUS_RUNNING, created_at=utc_now_iso())
    else:
        record = JobRecord(**raw)
        record.status = STATUS_RUNNING

    last_write = 0.0

    def update(stage: str, progress: int, message: str) -> None:
        nonlocal last_write
        record.stage = stage
        record.progress = max(0, min(100, progress))
        record.message = message

        # Avoid writing job files too frequently while still showing live updates.
        now = time.monotonic()
        if now - last_write >= 0.7 or progress in {0, 100}:
            save_job(record)
            last_write = now

    record.started_at = utc_now_iso()
    update("downloading", 1, "Preparing download")
    save_job(record)

    work_dir = job_work_dir(job_id)

    try:
        ref = parse_youtube_ref(url)
        if ref is None:
            raise PipelineError("Unable to parse canonical YouTube URL", code="input_parse_failed")

        if not record.media_title or not record.media_artist:
            cached = load_cached_metadata(ref.video_id)
            if cached:
                record.media_title = record.media_title or cached.get("title")
                record.media_artist = record.media_artist or cached.get("artist")
                record.media_thumbnail_url = record.media_thumbnail_url or cached.get("thumbnail_url")

        if not record.media_title or not record.media_artist:
            metadata = fetch_video_metadata(url)
            record.media_title = metadata.title
            record.media_artist = metadata.artist
            record.media_thumbnail_url = metadata.thumbnail_url
            save_cached_metadata(
                ref.video_id,
                {
                    "title": record.media_title,
                    "artist": record.media_artist,
                    "thumbnail_url": record.media_thumbnail_url or "",
                },
            )

        update("downloading", 2, f"Metadata loaded: {record.media_artist} - {record.media_title}")

        cached_cover = cached_thumbnail_path(ref.video_id) if has_cached_thumbnail(ref.video_id) else None
        if not cached_cover and record.media_thumbnail_url:
            cached_cover = cache_thumbnail_from_url(ref.video_id, record.media_thumbnail_url)

        output_name = build_output_filename(record.media_artist or "Unknown Artist", record.media_title or "Unknown Title")
        output_path = ensure_unique_output_path(settings.output_dir, output_name, job_id[:8])

        source, thumbnail_path = download_audio_source(
            url,
            work_dir,
            "source",
            preferred_thumbnail_path=cached_cover,
            concurrent_fragments=settings.download_concurrent_fragments,
            on_progress=lambda pct, note: update(
                "downloading",
                int(2 + (pct * 0.58)),
                note or f"Downloading source audio ({pct:.1f}%)",
            ),
        )
        update("extracting", 62, "Extracting audio stream")

        normalize_to_mp3_128k(
            source,
            output_path,
            target_i=target_i,
            mode=settings.normalization_mode,
            ffmpeg_threads=settings.ffmpeg_threads,
            title=record.media_title,
            artist=record.media_artist,
            album=record.media_title,
            cover_path=thumbnail_path,
            on_stage=lambda stage_message: update("normalizing", 70, stage_message),
            on_progress=lambda pct: update(
                "normalizing",
                int(72 + (pct * 0.26)),
                f"Normalizing and converting to MP3 ({pct:.1f}%)",
            ),
        )

        update("finalizing", 99, "Finalizing output")

        record.status = STATUS_COMPLETED
        record.stage = "completed"
        record.progress = 100
        record.finished_at = utc_now_iso()
        record.output_filename = output_path.name
        record.thumbnail_embedded = bool(thumbnail_path and thumbnail_path.exists())
        record.loudness_target = f"{target_i} LUFS"
        record.message = "Completed"
        save_job(record)
    except PipelineError as exc:
        record.status = STATUS_FAILED
        record.stage = "failed"
        record.finished_at = utc_now_iso()
        record.error_code = exc.code
        record.message = str(exc)
        save_job(record)
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
