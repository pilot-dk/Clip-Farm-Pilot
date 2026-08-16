from __future__ import annotations

import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from backend.app.vod import CachedVideoLibrary
from desktop_launcher import DesktopApi, _default_storage_dir


class CachedVideoLibraryTests(unittest.TestCase):
    def test_catalog_persists_and_delete_keeps_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uploads = root / "uploads"
            exports = root / "exports"
            trash = root / "trash"
            uploads.mkdir()
            exports.mkdir()
            video_id = "a" * 32
            source = uploads / f"{video_id}.mp4"
            source.write_bytes(b"cached source")
            exported = exports / "finished-clip.mp4"
            exported.write_bytes(b"finished export")

            library = CachedVideoLibrary(uploads, root / "library.json", trash)
            library.register(
                video_id,
                "Test YouTube VOD",
                "url",
                original_url="https://www.youtube.com/watch?v=test",
                duration=125.0,
                width=1920,
                height=1080,
            )

            reloaded = CachedVideoLibrary(uploads, root / "library.json", trash)
            items = reloaded.list_items()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["original_url"], "https://www.youtube.com/watch?v=test")
            self.assertEqual(items[0]["local_path"], str(source.resolve()))

            result = reloaded.move_to_trash(video_id)
            self.assertEqual(result["disposition"], "trash")
            self.assertFalse(source.exists())
            self.assertEqual(len(list(trash.glob("*.mp4"))), 1)
            self.assertTrue(exported.exists(), "deleting a source must never delete an exported clip")
            self.assertEqual(reloaded.list_items(), [])

    def test_discovers_older_cached_video(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uploads = root / "uploads"
            uploads.mkdir()
            video_id = "b" * 32
            (uploads / f"{video_id}.mov").write_bytes(b"legacy")

            library = CachedVideoLibrary(uploads, root / "library.json", root / "trash")
            items = library.list_items()
            self.assertEqual(items[0]["video_id"], video_id)
            self.assertEqual(items[0]["source_type"], "legacy")

    def test_rejects_invalid_video_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uploads = root / "uploads"
            uploads.mkdir()
            library = CachedVideoLibrary(uploads, root / "library.json", root / "trash")
            with self.assertRaises(KeyError):
                library.move_to_trash("../not-a-video")

    def test_default_library_uses_the_operating_system_trash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uploads = root / "uploads"
            uploads.mkdir()
            video_id = "e" * 32
            source = uploads / f"{video_id}.mp4"
            source.write_bytes(b"video")
            library = CachedVideoLibrary(uploads)
            library.register(video_id, "Trash test", "file")

            with patch("send2trash.send2trash") as send_to_trash:
                result = library.move_to_trash(video_id)

            self.assertEqual(result["disposition"], "trash")
            send_to_trash.assert_called_once_with(str(source.resolve()))
            self.assertEqual(library.list_items(), [])


class DesktopApiTests(unittest.TestCase):
    def test_reveal_video_uses_platform_file_manager_with_validated_cached_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uploads = root / "uploads"
            exports = root / "exports"
            uploads.mkdir()
            exports.mkdir()
            video_id = "c" * 32
            source = uploads / f"{video_id}.mp4"
            source.write_bytes(b"video")
            api = DesktopApi(exports, uploads)

            with patch("desktop_launcher.sys.platform", "darwin"), patch("desktop_launcher.subprocess.run") as run:
                result = api.reveal_video(video_id)

            self.assertEqual(result["status"], "revealed")
            run.assert_called_once_with(["open", "-R", str(source.resolve())], check=True, capture_output=True)

    def test_reveal_video_rejects_untrusted_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uploads = root / "uploads"
            exports = root / "exports"
            uploads.mkdir()
            exports.mkdir()
            api = DesktopApi(exports, uploads)
            with self.assertRaises(ValueError):
                api.reveal_video("../../private")

    def test_reveal_video_uses_windows_explorer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uploads = root / "uploads"
            exports = root / "exports"
            uploads.mkdir()
            exports.mkdir()
            video_id = "d" * 32
            source = uploads / f"{video_id}.mp4"
            source.write_bytes(b"video")
            api = DesktopApi(exports, uploads)

            with patch("desktop_launcher.sys.platform", "win32"), patch("desktop_launcher.subprocess.run") as run:
                api.reveal_video(video_id)

            run.assert_called_once_with(
                ["explorer", f"/select,{source.resolve()}"],
                check=True,
                capture_output=True,
            )

    def test_cross_platform_storage_locations(self):
        with patch("desktop_launcher.sys.platform", "win32"), patch.dict(
            os.environ,
            {"LOCALAPPDATA": "C:/Users/test/AppData/Local"},
            clear=True,
        ):
            self.assertEqual(
                _default_storage_dir(),
                Path("C:/Users/test/AppData/Local") / "Clip Farm Pilot",
            )

        with patch("desktop_launcher.sys.platform", "linux"), patch.dict(
            os.environ,
            {"XDG_DATA_HOME": "/home/test/.data"},
            clear=True,
        ):
            self.assertEqual(_default_storage_dir(), Path("/home/test/.data/clipfarmpilot"))


if __name__ == "__main__":
    unittest.main()
