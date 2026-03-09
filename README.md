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
- `./data/playlists` -> playlist JSON state
- `./data/output` -> generated MP3 files
- `./data/cache` -> metadata/thumbnail cache

## Run

```bash
docker-compose up --build
```

Open `http://localhost:8000`.

## Current Behavior

- Main intake accepts:
  - video URLs/IDs
  - playlist URLs/IDs
- Video input behavior:
  - normalized to canonical URL `https://www.youtube.com/watch?v=VIDEO_ID`
  - queued as extraction job
- Playlist input behavior (from main page):
  - upsert playlist record (add/update/restore)
  - auto-sync playlist metadata/entries
  - shows inline status message on main page
  - Playlists button receives temporary visual highlight cue
- Output profile:
  - MP3 (`128k CBR`)
  - loudness target `-14 LUFS` (default)
  - `NORMALIZATION_MODE=one_pass` default (speed)
- Metadata and output:
  - ID3 tags: `title`, `artist`, `album`
  - embeds thumbnail as cover art when available
  - filename format `ARTIST_ALBUM.mp3` (collision-safe suffix)
- Job UX (single main page):
  - running jobs with live progress (`stage`, `%`, message)
  - completed jobs list with incremental pagination:
    - initial 5
    - `Show more` reveals +10 each click
  - icon actions: download, open source, soft-delete
- Playlist UX (`/playlists` page):
  - add playlist
  - per-playlist actions: open source, sync, fetch (all / 10 newest), delete
  - expanding a playlist shows downloaded items + download buttons
  - fetch flow deduplicates already queued/running/completed streams
  - temporarily unavailable streams are skipped and retried on later fetch/sync

## API

- `GET /` converter UI
- `GET /playlists` playlist management UI
- `GET /downloads` redirects to `/` (legacy path)
- `GET /preview?url=...` metadata preview
- `GET /terms` Terms & Conditions page
- `GET /fair-usage` Fair Usage page
- `POST /jobs` enqueue conversion (`{ "url": "..." }`)
- `GET /jobs/{job_id}` job status
- `DELETE /jobs/{job_id}` soft-delete job entry
- `GET /download/{job_id}` download completed MP3
- `POST /playlists` add/update playlist (form)
- `POST /playlists/intake` add/update playlist from main-page JSON intake
- `POST /playlists/{playlist_id}/sync` sync playlist entries
- `POST /playlists/{playlist_id}/fetch-all` queue missing streams (`mode=all|newest10`)
- `POST /playlists/{playlist_id}/delete` soft-delete playlist

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
- `PLAYLISTS_DIR`
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
- `DOWNLOADS_PAGE_SIZE` (legacy/reserved)

## Operational Notes

- Expected intermittent provider-side breakages (anti-bot changes); retries are enabled.
- CAPTCHA bypass automation is intentionally out of scope.
- Soft-deleted jobs are hidden from listings and treated as not found in API download/status endpoints.
- License: MIT (see `LICENSE`).

## Roadmap

- Add provider abstraction for additional streaming sources if needed.
- Introduce optional provider modules (for example, YouTube + future providers) behind a consistent interface.
- Add richer filtering/analytics as usage grows.
- Add scheduled playlist sync jobs (optional auto-refresh).
