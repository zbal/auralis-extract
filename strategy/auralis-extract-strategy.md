# Auralis Extract Strategy (Current)

## 1. Goal and Scope

Build a single-user, containerized web app that extracts audio from supported video-streaming URLs to normalized MP3 with a minimal, reliable UX.

Current in-scope behavior:
- Accept provider URL input (currently YouTube implementation)
- Normalize input to canonical URL format
- Download audio, transcode to MP3, normalize loudness
- Embed metadata and thumbnail when available
- Track running/completed jobs on the main page with live progress
- Provide per-job actions: download output, open source URL, remove entry

Out of scope (current release):
- Account system / multi-tenant auth
- Captcha bypass tooling
- Playlist sync/download lifecycle (planned next phase)

## 2. Locked Product Decisions (Current)

- Backend: Python + FastAPI
- Queue: RQ + Redis
- Downloader: yt-dlp
- Media pipeline: ffmpeg/ffprobe
- Output profile: MP3 128k CBR
- Loudness target default: `-14 LUFS` (configurable)
- Deployment: Docker Compose (API + worker + Redis)
- Storage strategy: host bind mounts (`./data/*`) for backup/migration simplicity

## 3. Runtime Architecture

Services:
- `api`: FastAPI, server-rendered templates, job APIs
- `worker`: RQ worker executing media pipeline
- `redis`: queue backend

Storage mounts:
- `./data/jobs` -> `/data/jobs` (job JSON state)
- `./data/output` -> `/data/output` (generated MP3 files)
- `./data/cache` -> `/data/cache` (provider metadata/thumbnail cache)

## 4. Provider Strategy (Extensible)

Provider code is structured to support future sources:
- `app/providers/youtube/` contains YouTube parsing/validation/API behavior
- API routes use provider helpers for canonicalization and validation

Design intent:
- Keep provider-specific logic isolated
- Add future providers under `app/providers/<provider>/` with similar shape

## 5. Input Canonicalization and Request Hygiene

Current behavior:
- Accept full provider links and raw provider IDs (YouTube: 11-char video ID)
- Strip extraneous query parameters and keep canonical identity only
- Canonical form (YouTube): `https://www.youtube.com/watch?v=VIDEO_ID`
- Reject invalid/non-supported input early

Anti-spam and efficiency controls:
- Frontend debounced metadata prefetch
- In-memory + disk metadata cache by video ID
- In-flight request deduplication per video ID
- No continuous polling when no active jobs remain

## 6. Processing Pipeline (Worker)

1. Resolve canonical URL + provider identity
2. Load metadata from cache or fetch via provider/downloader
3. Download best audio stream
4. Normalize + transcode to MP3
5. Write ID3 tags and embed cover art when available
6. Persist job updates by stage/progress
7. Move output to mounted output storage and cleanup temp files

Output naming:
- Sanitized format: `ARTIST_ALBUM.mp3`
- Collision-safe suffix when required

Metadata mapping:
- `title`: media title
- `artist`: uploader/channel/provider author
- `album`: media title
- cover art: thumbnail image when available

## 7. Loudness and Performance Strategy

Normalization:
- `loudnorm` target default `-14 LUFS`
- configurable via environment

Performance modes:
- `NORMALIZATION_MODE=one_pass` (default, faster)
- `NORMALIZATION_MODE=two_pass` (more precise, slower)

Performance controls:
- `FFMPEG_THREADS` (`0` = ffmpeg auto)
- `DOWNLOAD_CONCURRENT_FRAGMENTS`
- `JOB_TIMEOUT_SECONDS` high enough for long files
- Optional multiple workers for throughput (concurrent jobs), not single-file acceleration

## 8. UI Strategy (Current)

Main page (`/`) only:
- Brand header + short CTA
- URL input with inline `Extract` button
- Enter key on URL input triggers submit
- Running jobs section appears only when active jobs exist
- Completed jobs section always available
- Job cards show thumbnail/title/author/status/stage/progress
- Job actions: download, source link, remove

Removed UX elements (intentionally):
- Separate downloads page workflow
- Similar-files suggestions
- In-page “detected media” preview panel
- Search/filter bar

## 9. API Surface (Current)

- `GET /` : main UI
- `GET /downloads` : redirect to `/`
- `GET /preview?url=...` : metadata prefetch endpoint
- `POST /jobs` : enqueue extraction job
- `GET /jobs/{job_id}` : job state
- `DELETE /jobs/{job_id}` : soft-delete job entry
- `GET /download/{job_id}` : download output when completed
- `GET /terms` : Terms page
- `GET /fair-usage` : Fair Usage page

## 10. Data and Retention

Job record includes:
- IDs/timestamps
- input/canonical URL
- status/stage/progress/message
- media metadata (title, artist, thumbnail)
- output filename/path metadata
- error code/message when failed
- soft-delete marker (`removed`)

Cache model:
- metadata and thumbnail cache keyed by video ID
- reused by preview API and worker to reduce repeated upstream requests

## 11. Operational Guardrails

- No captcha-bypass implementation
- Early input validation + canonicalization
- Retry policy for transient failures
- Atomic job file writes and tolerant reads
- Soft-delete in UI/history without destructive source manipulation

## 12. Related Strategies

Playlist management is tracked separately in:
- `strategy/playlist-management-strategy.md`
