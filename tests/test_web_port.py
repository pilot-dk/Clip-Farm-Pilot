from __future__ import annotations

import base64
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from starlette.requests import Request

from backend.app import main
from backend.app.vod import CachedVideoLibrary


def png_data_url(size: tuple[int, int] = (1080, 1080)) -> str:
    output = io.BytesIO()
    Image.new("RGBA", size, (0, 0, 0, 0)).save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


class WebPortTests(unittest.TestCase):
    def test_accepts_browser_rendered_square_caption(self):
        overlay = main._caption_overlay_from_data_url(png_data_url())
        self.assertIsNotNone(overlay)
        try:
            with Image.open(overlay) as image:
                self.assertEqual(image.size, (1080, 1080))
                self.assertEqual(image.format, "PNG")
        finally:
            overlay.unlink(missing_ok=True)

    def test_rejects_wrong_caption_dimensions(self):
        with self.assertRaisesRegex(ValueError, "1080"):
            main._caption_overlay_from_data_url(png_data_url((512, 512)))

    def test_signed_session_cookie_authenticates(self):
        expires = 4_102_444_800
        with patch.object(main, "WEB_PASSWORD", "private"), patch.object(main, "SESSION_SECRET", "test-secret"):
            token = f"{expires}.{main._session_signature(expires)}"
            request = Request({"type": "http", "method": "GET", "path": "/api/library/videos", "headers": [(b"cookie", f"clipfarmpilot_session={token}".encode())]})
            self.assertTrue(main._authenticated(request))

    def test_cloud_mode_permanently_deletes_working_source(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"CLIPFARMPILOT_DELETE_PERMANENT": "1"}):
            root = Path(temporary)
            uploads = root / "uploads"
            uploads.mkdir()
            video_id = "e" * 32
            source = uploads / f"{video_id}.mp4"
            source.write_bytes(b"temporary source")
            library = CachedVideoLibrary(uploads, root / "library.json", root / "trash")
            library.register(video_id, "Cloud VOD", "url")

            result = library.move_to_trash(video_id)

            self.assertEqual(result["disposition"], "deleted")
            self.assertFalse(source.exists())
            self.assertFalse((root / "trash").exists())


if __name__ == "__main__":
    unittest.main()
