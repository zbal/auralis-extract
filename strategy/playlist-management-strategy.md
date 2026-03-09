# Playlist Management Strategy

## 1. Objective

Add playlist support with incremental synchronization so users can download only new items as they become available.

Primary outcomes:
- Accept playlist URLs
- Keep a local playlist cache/index
- Detect newly added entries
- Queue downloads only for new entries (or selected states)

## 2. Scope Boundaries

In scope:
- Playlist registration and metadata sync
- Item-level state tracking (`new`, `queued`, `downloaded`, `failed`, etc.)
- Manual and optional scheduled sync
- "Download new" operation

Out of scope (initial phase):
- Multi-user permissions model
- Full DB migration at day one (JSON index first is acceptable)
- Provider-specific bypass mechanisms

## 3. Data Model

Add playlist index storage (JSON first, SQLite migration path):

- `playlist`:
  - `playlist_id`
  - `provider`
  - `playlist_url`
  - `title`
  - `status`
  - `last_sync_at`
  - `next_sync_at`
  - `auto_sync_enabled`
  - `sync_interval_minutes`

- `playlist_item`:
  - `playlist_id`
  - `video_id`
  - `title`
  - `artist`
  - `published_at`
  - `position`
  - `state` in `{new, queued, downloaded, failed, skipped, removed}`
  - `last_seen_at`
  - `last_job_id`
  - `error`

Uniqueness rule:
- `(playlist_id, video_id)`

## 4. Sync Workflow

1. Resolve playlist URL into canonical playlist identity.
2. Fetch playlist entries (metadata-only pass).
3. Upsert entries into local index.
4. Mark previously known but now missing entries as `removed` (soft state).
5. Mark newly discovered entries as `new`.
6. If auto-download is enabled, queue all `new` entries.

## 5. Download Integration

- Reuse existing single-item extraction job pipeline.
- Attach `playlist_id` + `video_id` metadata to each job.
- On success: item state -> `downloaded`.
- On failure: item state -> `failed` with retry/error info.
- Idempotency: do not queue the same playlist item if already active.

## 6. API Plan

Proposed endpoints:
- `POST /playlists` (register playlist)
- `POST /playlists/{id}/sync` (manual sync)
- `POST /playlists/{id}/download-new` (queue all `new`)
- `GET /playlists/{id}` (playlist summary)
- `GET /playlists/{id}/items` (paginated items)

## 7. UI Plan

Add playlist block to main page (or dedicated section):
- playlist URL input
- actions: `Sync now`, `Download new`, `Retry failed`
- optional `Auto-sync` toggle
- counters: `total`, `new`, `downloaded`, `failed`
- state badges per item

## 8. Caching and Throttling

- Reuse metadata/thumbnail cache by video ID.
- Add sync interval floor (example: >= 10 minutes).
- Apply per-playlist and global backoff on provider errors.
- Use incremental sync to avoid full repeated downloads.

## 9. Reliability and Safety

- Keep operations idempotent.
- Persist state transitions atomically.
- Preserve removed/missing items as soft states for auditability.
- Keep legal/fair-usage reminders visible in docs and UI.

## 10. Rollout Order

1. Playlist data model + local index persistence.
2. Manual sync endpoint and item diffing.
3. `Download new` queue integration.
4. UI controls and playlist visibility.
5. Optional auto-sync scheduler.
6. Optional migration from JSON index to SQLite.
