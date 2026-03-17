# Playlist Management Strategy

## 1. Objective

Add playlist support with incremental synchronization so users can see the full stream inventory, distinguish downloaded vs. not-yet-downloaded items at a glance, and queue only the streams they want.

Primary outcomes:
- Accept playlist URLs
- Keep a local playlist cache/index
- Detect newly added entries
- Show all known playlist streams with metadata
- Queue downloads only for selected entries or "new" entries

## 2. Scope Boundaries

In scope:
- Playlist registration and metadata sync
- Item-level state tracking (`new`, `queued`, `downloaded`, `failed`, etc.)
- Manual and optional scheduled sync
- Stream-level multi-select for queueing
- "Download new" operation
- Visual differentiation for `downloaded`, `queued`, `new`, and `not downloaded`

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
  - `sync_revision`
  - `next_sync_at`
  - `auto_sync_enabled`
  - `sync_interval_minutes`

- `playlist_item`:
  - `playlist_id`
  - `video_id`
  - `title`
  - `artist`
  - `thumbnail_url`
  - `published_at`
  - `position`
  - `state` in `{new, queued, downloaded, failed, skipped, removed}`
  - `discovered_sync_revision`
  - `last_seen_sync_revision`
  - `last_seen_at`
  - `last_job_id`
  - `error`

Uniqueness rule:
- `(playlist_id, video_id)`

## 4. Sync Workflow

1. Resolve playlist URL into canonical playlist identity.
2. Increment playlist `sync_revision`.
3. Fetch playlist entries (metadata-only pass).
4. Upsert entries into local index.
5. For existing entries, update metadata and `last_seen_sync_revision`.
6. For newly discovered entries, set `state=new` and `discovered_sync_revision=current_sync_revision`.
7. Mark previously known but now missing entries as `removed` (soft state).
8. If auto-download is enabled, queue all `new` entries.

Newness rule:
- An item is highlighted as "new on latest sync" when `discovered_sync_revision == playlist.sync_revision`.
- This makes new-item highlighting deterministic and avoids depending on time-window heuristics.

## 5. Download Integration

- Reuse existing single-item extraction job pipeline.
- Attach `playlist_id` + `video_id` metadata to each job.
- On success: item state -> `downloaded`.
- On failure: item state -> `failed` with retry/error info.
- Idempotency: do not queue the same playlist item if already active.

Queueing rules:
- Allow queueing from explicit stream selection, not only playlist-wide actions.
- Disable selection for items already `queued` or `running`.
- Keep `downloaded` items visible and in full color, but unselected by default.
- Keep not-yet-downloaded items selectable even when greyed out.

## 6. API Plan

Proposed endpoints:
- `POST /playlists` (register playlist)
- `POST /playlists/{id}/sync` (manual sync)
- `POST /playlists/{id}/download-new` (queue all `new`)
- `POST /playlists/{id}/queue-selected` (queue explicit stream IDs)
- `GET /playlists/{id}` (playlist summary)
- `GET /playlists/{id}/items` (paginated items)

## 7. UI Plan

Add playlist block to main page (or dedicated section):
- playlist URL input
- actions: `Sync now`, `Fetch new`, `Add selected to queue`, `Retry failed`
- optional `Auto-sync` toggle
- counters: `total`, `new`, `downloaded`, `queued`, `failed`
- state badges per item

Per-stream presentation:
- Show every known playlist stream, not only downloaded ones.
- Display `title`, `artist`, and `thumbnail` for each stream.
- `downloaded`: full color card/row with download action.
- `queued`: full color with queued badge.
- `new`: green-tinted highlight only when the stream was introduced by the latest sync delta.
- `not downloaded`: visually muted/greyed, still readable and selectable, with no special badge.
- `unavailable`: muted and not selectable, with a short reason label.

Selection UX recommendation:
- Use a checkbox per stream plus a sticky batch-action bar inside the expanded playlist panel.
- Include quick actions: `Select new`, `Select undownloaded`, `Clear`.
- Submit selected stream IDs in one request to create download jobs.
- Add a per-stream queue icon for single-item queueing without batch selection.

Recommendation on sync behavior:
- Do not auto-queue newly synced items by default.
- Keep default page-load selection empty.
- Immediately after a successful sync, pre-select only the newly discovered streams.
- Keep a one-click `Fetch new` action for users who want immediate queueing.
- This preserves speed for the common case while avoiding accidental downloads when a sync surfaces many new entries unexpectedly.

Throughput guardrail:
- Do not trigger per-stream metadata refreshes during sync.
- Reuse playlist-level metadata and local cache whenever possible.
- If individual stream revalidation is ever required during queue preparation, do it sequentially with a small delay between upstream calls.

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
3. Per-stream derived state (`downloaded`, `queued`, `new`, `undownloaded`, `unavailable`).
4. `Download new` and `queue selected` integration.
5. UI inventory list with checkboxes, metadata, and color states.
6. Optional auto-sync scheduler.
7. Optional migration from JSON index to SQLite.
