import os
import tempfile
import unittest
from unittest.mock import patch

_TEST_ROOT = tempfile.mkdtemp(prefix="auralis-tests-")
os.environ.setdefault("JOBS_DIR", os.path.join(_TEST_ROOT, "jobs"))
os.environ.setdefault("PLAYLISTS_DIR", os.path.join(_TEST_ROOT, "playlists"))
os.environ.setdefault("OUTPUT_DIR", os.path.join(_TEST_ROOT, "output"))
os.environ.setdefault("TEMP_DIR", os.path.join(_TEST_ROOT, "tmp"))
os.environ.setdefault("METADATA_CACHE_DIR", os.path.join(_TEST_ROOT, "cache", "metadata"))
os.environ.setdefault("THUMBNAIL_CACHE_DIR", os.path.join(_TEST_ROOT, "cache", "thumbnails"))

from app.services import playlist_service


class PlaylistServiceTests(unittest.TestCase):
    def test_first_revision_aware_sync_does_not_mark_legacy_streams_as_new(self) -> None:
        playlist = {
            "playlist_id": "p1",
            "source_url": "https://example.test/playlist",
            "title": "Legacy Playlist",
            "last_sync_at": "2026-03-01T00:00:00+00:00",
            "streams": [
                {
                    "video_id": "old-1",
                    "url": "https://www.youtube.com/watch?v=old-1",
                    "title": "Old One",
                    "artist": "Artist",
                }
            ],
        }
        payload = {
            "title": "Legacy Playlist",
            "entries": [
                {"id": "old-1", "title": "Old One", "uploader": "Artist"},
                {"id": "new-1", "title": "New One", "uploader": "Artist"},
            ],
        }

        with patch.object(playlist_service, "fetch_playlist_metadata", return_value=payload), patch.object(
            playlist_service, "save_playlist"
        ):
            synced, error = playlist_service.sync_playlist_record(playlist)

        self.assertTrue(synced)
        self.assertEqual(error, "")
        self.assertEqual(playlist["sync_revision"], 1)

        streams = {stream["video_id"]: stream for stream in playlist["streams"]}
        self.assertEqual(streams["old-1"]["discovered_sync_revision"], 0)
        self.assertEqual(streams["new-1"]["discovered_sync_revision"], 1)
        self.assertEqual(playlist_service.count_new_streams(playlist), 1)

    def test_subsequent_sync_marks_only_latest_delta_as_new(self) -> None:
        playlist = {
            "playlist_id": "p2",
            "source_url": "https://example.test/playlist",
            "title": "Tracked Playlist",
            "last_sync_at": "2026-03-10T00:00:00+00:00",
            "sync_revision": 1,
            "streams": [
                {
                    "video_id": "old-1",
                    "url": "https://www.youtube.com/watch?v=old-1",
                    "title": "Old One",
                    "artist": "Artist",
                    "discovered_sync_revision": 0,
                    "last_seen_sync_revision": 1,
                },
                {
                    "video_id": "new-1",
                    "url": "https://www.youtube.com/watch?v=new-1",
                    "title": "New One",
                    "artist": "Artist",
                    "discovered_sync_revision": 1,
                    "last_seen_sync_revision": 1,
                },
            ],
        }
        payload = {
            "title": "Tracked Playlist",
            "entries": [
                {"id": "old-1", "title": "Old One", "uploader": "Artist"},
                {"id": "new-1", "title": "New One", "uploader": "Artist"},
                {"id": "new-2", "title": "Newest One", "uploader": "Artist"},
            ],
        }

        with patch.object(playlist_service, "fetch_playlist_metadata", return_value=payload), patch.object(
            playlist_service, "save_playlist"
        ):
            synced, error = playlist_service.sync_playlist_record(playlist)

        self.assertTrue(synced)
        self.assertEqual(error, "")
        self.assertEqual(playlist["sync_revision"], 2)

        streams = {stream["video_id"]: stream for stream in playlist["streams"]}
        self.assertEqual(streams["old-1"]["discovered_sync_revision"], 0)
        self.assertEqual(streams["new-1"]["discovered_sync_revision"], 1)
        self.assertEqual(streams["new-2"]["discovered_sync_revision"], 2)
        self.assertEqual(playlist_service.count_new_streams(playlist), 1)

        views = playlist_service.build_playlist_views([playlist], records=[])
        self.assertEqual(len(views), 1)
        self.assertEqual(views[0]["new_count"], 1)
        states = {stream["video_id"]: stream["state"] for stream in views[0]["stream_items"]}
        self.assertEqual(states["old-1"], "undownloaded")
        self.assertEqual(states["new-1"], "undownloaded")
        self.assertEqual(states["new-2"], "new")

    def test_sync_summary_tracks_added_removed_and_availability_changes(self) -> None:
        previous = [
            {
                "video_id": "keep",
                "availability_status": "available",
            },
            {
                "video_id": "gone",
                "availability_status": "available",
            },
            {
                "video_id": "private-now",
                "availability_status": "available",
            },
            {
                "video_id": "back-now",
                "availability_status": "temporarily_unavailable",
            },
        ]
        current = [
            {
                "video_id": "keep",
                "availability_status": "available",
            },
            {
                "video_id": "private-now",
                "availability_status": "temporarily_unavailable",
            },
            {
                "video_id": "back-now",
                "availability_status": "available",
            },
            {
                "video_id": "added",
                "availability_status": "available",
            },
        ]

        summary = playlist_service.summarize_sync_changes(previous, current)
        self.assertEqual(
            summary,
            {
                "added": 1,
                "removed": 1,
                "became_unavailable": 1,
                "became_available": 1,
            },
        )


if __name__ == "__main__":
    unittest.main()
