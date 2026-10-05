from __future__ import annotations

import asyncio
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from backend.app import main
from backend.app.video import MAX_CLIP_CANDIDATES
from desktop_launcher import DesktopApi


class ExportArchiveTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.exports = Path(folder.name) / "exports"
        self.exports.mkdir()
        self.ids = []
        for index in range(3):
            export_id = f"{index:x}" * 32
            (self.exports / f"{export_id}.mp4").write_bytes(f"clip {index}".encode())
            self.ids.append(export_id)
        patcher = patch.object(main, "EXPORTS", self.exports)
        patcher.start()
        self.addCleanup(patcher.stop)

    def archive(self, items):
        return main.export_archive(main.ExportArchiveRequest(items=items))

    def test_every_clip_is_stored_under_its_title_and_the_zip_is_removed_after_sending(self):
        response = self.archive([
            {"export_id": self.ids[0], "filename": "Wait for the Payoff.mp4"},
            {"export_id": self.ids[1], "filename": "Wait for the payoff.mp4"},
            {"export_id": self.ids[2], "filename": "Clutch: 1 v 4?"},
        ])
        archive = Path(response.path)

        self.assertEqual(response.media_type, "application/zip")
        self.assertIn("Clip%20Farm%20Pilot%20clips.zip", response.headers["content-disposition"])
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            self.assertEqual(names, ["Wait for the Payoff.mp4", "Wait for the payoff (2).mp4", "Clutch 1 v 4.mp4"])
            self.assertEqual(bundle.read(names[2]), b"clip 2")
            self.assertTrue(all(info.compress_type == zipfile.ZIP_STORED for info in bundle.infolist()))
        asyncio.run(response.background())
        self.assertFalse(archive.exists())

    def test_a_missing_export_is_refused(self):
        (self.exports / f"{self.ids[1]}.mp4").unlink()
        with self.assertRaises(HTTPException) as raised:
            self.archive([{"export_id": self.ids[0], "filename": "a.mp4"}, {"export_id": self.ids[1], "filename": "b.mp4"}])
        self.assertEqual(raised.exception.status_code, 404)

    def test_only_export_ids_and_at_most_thirty_clips_are_accepted(self):
        for items in (
            [{"export_id": "../../secret", "filename": "a.mp4"}],
            [],
            [{"export_id": self.ids[0], "filename": "a.mp4"}] * (MAX_CLIP_CANDIDATES + 1),
        ):
            with self.assertRaises(ValidationError):
                main.ExportArchiveRequest(items=items)


class DesktopBulkSaveTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.exports = self.root / "exports"
        self.exports.mkdir()
        self.items = []
        for index, name in enumerate(("Big Finish.mp4", "Big Finish.mp4", "No Way")):
            export_id = f"{index + 10:02x}" * 16
            (self.exports / f"{export_id}.mp4").write_bytes(f"clip {index}".encode())
            self.items.append({"export_id": export_id, "filename": name})
        self.api = DesktopApi(self.exports)
        self.window = MagicMock()
        self.api._bind_window(self.window, save_dialog_type=30, folder_dialog_type=20)

    def test_one_folder_choice_saves_every_clip_without_overwriting(self):
        folder = self.root / "picked"
        folder.mkdir()
        (folder / "No Way.mp4").write_bytes(b"already here")
        self.window.create_file_dialog.return_value = (str(folder),)

        result = self.api.save_exports(self.items)

        self.assertEqual(self.window.create_file_dialog.call_count, 1)
        self.assertEqual(self.window.create_file_dialog.call_args.args[0], 20)
        self.assertEqual(result["status"], "saved")
        self.assertEqual(result["count"], 3)
        self.assertEqual(
            [Path(path).name for path in result["paths"]],
            ["Big Finish.mp4", "Big Finish (2).mp4", "No Way (2).mp4"],
        )
        self.assertEqual((folder / "No Way.mp4").read_bytes(), b"already here")
        self.assertEqual((folder / "Big Finish (2).mp4").read_bytes(), b"clip 1")

    def test_cancelling_the_folder_choice_saves_nothing(self):
        # Cocoa and GTK report a cancel as None, Qt as an empty path.
        for cancelled in (None, (), ("",)):
            self.window.create_file_dialog.return_value = cancelled
            self.assertEqual(self.api.save_exports(self.items), {"status": "cancelled"})

    def test_untrusted_or_missing_clips_are_refused_before_asking_for_a_folder(self):
        with self.assertRaises(ValueError):
            self.api.save_exports([{"export_id": "../escape", "filename": "x.mp4"}])
        with self.assertRaises(FileNotFoundError):
            self.api.save_exports([{"export_id": "f" * 32, "filename": "x.mp4"}])
        with self.assertRaises(ValueError):
            self.api.save_exports([])
        self.window.create_file_dialog.assert_not_called()


if __name__ == "__main__":
    unittest.main()
