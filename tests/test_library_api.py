from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.app.vod import CachedVideoLibrary
from backend.app.video import VideoInfo

class VideoLibraryApiTests(unittest.TestCase):
    def test_list_and_delete_cached_video_handlers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            from backend.app import main

            storage = root / "storage"
            uploads = storage / "uploads"
            exports = storage / "exports"
            trash = root / "trash"
            uploads.mkdir(parents=True)
            exports.mkdir(parents=True)
            library = CachedVideoLibrary(uploads, storage / "video_library.json", trash)
            video_id = "d" * 32
            source = uploads / f"{video_id}.mp4"
            source.write_bytes(b"test source")
            library.register(video_id, "API test VOD", "url", "https://youtu.be/test")
            exported = exports / "kept-export.mp4"
            exported.write_bytes(b"keep me")

            with patch.object(main, "UPLOADS", uploads), patch.object(main, "EXPORTS", exports), patch.object(main, "VIDEO_LIBRARY", library):
                listed = main.list_cached_videos()
                item = next(value for value in listed["items"] if value["video_id"] == video_id)
                self.assertEqual(item["original_url"], "https://youtu.be/test")

                with patch("backend.app.vod.probe_video", return_value=VideoInfo(1920, 1080, 123.4)):
                    selected = main.select_cached_video(video_id)
                self.assertEqual(selected["video_id"], video_id)
                self.assertEqual(selected["source_url"], f"/api/videos/{video_id}/source")
                self.assertEqual(selected["duration"], 123.4)

                deleted = main.delete_cached_video(video_id)
                self.assertEqual(deleted["disposition"], "trash")
            self.assertFalse(source.exists())
            self.assertTrue(exported.exists())


if __name__ == "__main__":
    unittest.main()
