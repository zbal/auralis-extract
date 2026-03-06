# Auralis Extract Strategy (As Implemented)

## 1. Goal and Scope

Build a single-user web app that converts YouTube videos to normalized MP3 with a simple operational UX.

Current in-scope behavior:
- Accept YouTube input as full URL or raw video ID
- Normalize and canonicalize input to one URL shape
- Download audio, transcode to MP3, normalize loudness
- Embed metadata and thumbnail when available
- Track jobs with live stage/progress updates
- Keep download history with actions (download, source, remove)

Out of scope:
- Account system / multi-tenant auth
- Captcha bypass tooling
- Full playlist workflow (future phase)

## 2. Locked Product Decisions (Current)

- Backend: Python + FastAPI
- Queue: RQ + Redis
- Downloader: yt-dlp
- Media pipeline: ffmpeg/ffprobe
- Output: MP3 128k CBR
- Loudness target default: `-14 LUFS` (configurable)
- Deployment mode: single-user, containerized
- Storage: host bind mounts (`./data/*`) for easy backup/migration

## 3. Runtime Architecture

Services (docker-compose):
- `api`: FastAPI + HTML templates
- `worker`: background RQ worker for media processing
- `redis`: queue backend

Storage mounts:
- `./data/jobs` -> `/data/jobs` (job JSON state)
- `./data/output` -> `/data/output` (MP3 outputs)
- `./data/cache` -> `/data/cache` (metadata/thumbnail cache)

## 4. Input and URL Normalization Strategy

Input accepted:
- Full YouTube links (`youtube.com`, `youtu.be`, `shorts`, `embed`, extra query params)
- Raw 11-character YouTube video ID

Normalization behavior:
- Extract only valid video ID
- Reject invalid/non-YouTube inputs early
- Canonicalize to: `https://www.youtube.com/watch?v=VIDEO_ID`

Anti-spam behavior:
- Frontend does local validation before preview request
- Debounced preview fetch (delayed query)
- API in-memory preview cache + disk cache reuse

## 5. Processing Pipeline (Worker)

1. Resolve canonical URL + video ID
2. Load metadata from cache, else fetch via yt-dlp
3. Download best audio via yt-dlp
4. Normalize + transcode to MP3 via ffmpeg
5. Write ID3 tags and embed cover art when available
6. Persist job state and cleanup temp files

Output naming:
- Sanitized format: `ARTIST_ALBUM.mp3`
- Collision-safe suffix when needed

Metadata tagging:
- `title`: YouTube title
- `artist`: uploader/channel
- `album`: YouTube title
- cover art: video thumbnail when available

## 6. Loudness and Performance Strategy

Normalization:
- `loudnorm` with target LUFS (configurable via env)
- Default target: `-14 LUFS`

Performance modes:
- `NORMALIZATION_MODE=one_pass` (default; faster)
- `NORMALIZATION_MODE=two_pass` (slower; higher precision)

Additional speed knobs:
- `FFMPEG_THREADS` (default `0` = ffmpeg auto)
- `DOWNLOAD_CONCURRENT_FRAGMENTS` (default `4`)

Queue timeout:
- `JOB_TIMEOUT_SECONDS` default `7200` for long media

## 7. UI Strategy

Converter page (`/`):
- URL input + preview panel (title/artist/thumbnail)
- Submit job
- Live status (`stage`, `% progress`, message)
- Similar previous files section

Downloads page (`/downloads`):
- Ongoing jobs section with progress bars and polling
- Completed jobs section
- Right-side icon actions per item:
  - download
  - open source URL
  - soft-delete entry
- Polling stops when no active jobs remain

## 8. API Surface (Current)

- `GET /` : converter UI
- `GET /downloads` : history UI
- `GET /preview?url=...` : metadata preview
- `GET /similar?artist=...&title=...&exclude_url=...` : related history
- `POST /jobs` : enqueue conversion
- `GET /jobs/{job_id}` : job state
- `DELETE /jobs/{job_id}` : soft-delete job entry
- `GET /download/{job_id}` : download MP3 when completed

## 9. Data Model and Retention

Job JSON includes:
- identity and timing (`job_id`, `created_at`, etc.)
- input/canonical URL
- status/stage/progress/message
- media metadata (title, artist, thumbnail URL)
- output filename
- loudness target, error code
- `removed` flag for soft-delete

Caching:
- Metadata cache by video ID
- Thumbnail cache by video ID
- Reused by both API and worker to reduce provider calls

## 10. Operational Guardrails

- No captcha bypass implementation
- Retry policy for transient job failures
- Input/domain validation and canonicalization
- Filename sanitization
- Job soft-delete in UI (record hidden from listings)

## 11. Known Tradeoffs

- Single-job speed is bounded by media decode/filter/encode path
- Multiple workers improve throughput for multiple jobs, not a single-file latency jump
- `one_pass` normalization is faster but less exact than two-pass

## 12. Next Evolution Path

1. Playlist resolver layer (parent/child jobs)
2. Retention policy + cleanup job (old outputs/cache)
3. Optional SQLite index for richer filtering/search
4. Optional SSE/WebSocket updates (replace polling)
5. Optional quality/profile presets in UI (fast vs precise)
