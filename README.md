# auralis-extract

<img src="app/web/static/logo.png" alt="Auralis Extract logo" width="120" />

Containerized single-user video-streaming platform to MP3 service with loudness normalization.

## Why This Project

This project was built for a narrow personal use case:
- I use swimming headphones that only support MP3 files.
- Bluetooth is not usable in the pool.
- I like listening to podcast content available on YouTube.
- Existing web-based “YouTube to MP3” converters are usually ad-heavy and low quality.

## Legal Notice and Intended Use

- This project is **not affiliated with, endorsed by, or sponsored by YouTube/Google**.
- You are responsible for ensuring your use complies with:
  - applicable copyright laws in your jurisdiction
  - YouTube Terms of Service and any other platform terms
- Do not use this project to infringe copyrights or bypass technical protections.
- Maintainers/contributors do not provide legal advice and are not responsible for user misuse.
- This project is intended for lawful personal use.

## Architecture

Services (docker-compose):
- `api`: FastAPI web UI + HTTP endpoints
- `worker`: RQ background media pipeline
- `redis`: queue backend

Why multi-container:
- Keeps UI responsive while long media jobs run in background
- Worker can scale independently for higher throughput

## Storage

Host-mounted by default:
- `./data/jobs` -> job JSON state
- `./data/output` -> generated MP3 files
- `./data/cache` -> metadata/thumbnail cache

## Run

```bash
docker-compose up --build
```

Open `http://localhost:8000`.

## Current Behavior

- Input accepts:
  - full YouTube URLs (`youtube.com`, `youtu.be`, `shorts`, `embed`, extra params)
  - raw 11-char YouTube video ID
- Input is normalized to canonical URL:
  - `https://www.youtube.com/watch?v=VIDEO_ID`
- Preview fetch shows title/artist/thumbnail
- Output: MP3 (`128k CBR`)
- Loudness normalization default:
  - target `-14 LUFS`
  - `NORMALIZATION_MODE=one_pass` (speed default)
- Progress telemetry:
  - `stage` + `% progress` + status message
- ID3 tags:
  - `title`, `artist`, `album`
- Thumbnail embedding:
  - attempts to embed video thumbnail as cover art
- Output filename:
  - normalized `ARTIST_ALBUM.mp3` with collision-safe suffix
- Main page includes running + past jobs in one list:
  - live progress updates for active jobs
  - icon actions: download, open source, soft-delete
  - in-page search/filter as you type
- Similar files section on converter page based on artist/title matching

## API

- `GET /` converter UI
- `GET /downloads` redirects to `/` (legacy path)
- `GET /preview?url=...` metadata preview
- `GET /similar?artist=...&title=...&exclude_url=...` related previous files
- `GET /terms` Terms & Conditions page
- `GET /fair-usage` Fair Usage page
- `POST /jobs` enqueue conversion (`{ "url": "..." }`)
- `GET /jobs/{job_id}` job status
- `DELETE /jobs/{job_id}` soft-delete job entry
- `GET /download/{job_id}` download completed MP3

## Performance Tuning

Current speed-oriented defaults:
- `NORMALIZATION_MODE=one_pass`
- `FFMPEG_THREADS=0` (ffmpeg auto threads)
- `DOWNLOAD_CONCURRENT_FRAGMENTS=4`
- `JOB_TIMEOUT_SECONDS=7200`

For maximum loudness precision (slower), set:
- `NORMALIZATION_MODE=two_pass`

To improve throughput for multiple jobs:
```bash
docker-compose up --build --scale worker=2
```

Note: multiple workers improve concurrency/queue time, not single-file latency.

## Configuration (Environment)

Main env vars used by `api`/`worker`:
- `REDIS_URL`
- `JOBS_DIR`
- `OUTPUT_DIR`
- `TEMP_DIR`
- `METADATA_CACHE_DIR`
- `THUMBNAIL_CACHE_DIR`
- `MAX_JOB_RETRIES`
- `JOB_TIMEOUT_SECONDS`
- `TARGET_LUFS`
- `NORMALIZATION_MODE` (`one_pass|two_pass`)
- `FFMPEG_THREADS`
- `DOWNLOAD_CONCURRENT_FRAGMENTS`
- `GITHUB_URL` (used in footer link)
- `DOWNLOADS_PAGE_SIZE` (reserved for history pagination behavior)

## Operational Notes

- Expected intermittent provider-side breakages (anti-bot changes); retries are enabled.
- CAPTCHA bypass automation is intentionally out of scope.
- Soft-deleted jobs are hidden from listings and treated as not found in API download/status endpoints.
- License: MIT (see `LICENSE`).

## Roadmap

- Add provider abstraction for additional streaming sources if needed.
- Introduce optional provider modules (for example, YouTube + future providers) behind a consistent interface.
- Add richer filtering/analytics in downloads history as usage grows.
