from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from backend.app.social import SocialPublisher
from desktop_launcher import DesktopApi


class SocialAccountTests(unittest.TestCase):
    def test_connection_url_uses_official_youtube_oauth_and_local_callback(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            "os.environ",
            {
                "CLIPFARMPILOT_YOUTUBE_CLIENT_ID": "youtube-client",
                "CLIPFARMPILOT_YOUTUBE_CLIENT_SECRET": "youtube-secret",
            },
            clear=True,
        ):
            root = Path(temporary)
            publisher = SocialPublisher(root, root)
            result = publisher.start_connection("youtube", "http://127.0.0.1:8765")

        self.assertEqual(result["status"], "authorize")
        parsed = urlparse(result["auth_url"])
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.hostname, "accounts.google.com")
        self.assertEqual(query["client_id"], ["youtube-client"])
        self.assertEqual(query["redirect_uri"], ["http://127.0.0.1:8765/api/social/youtube/callback"])
        self.assertIn("https://www.googleapis.com/auth/youtube.upload", query["scope"])

    def test_unconfigured_platform_returns_setup_instead_of_fake_connection(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict("os.environ", {}, clear=True):
            root = Path(temporary)
            publisher = SocialPublisher(root, root)
            result = publisher.start_connection("instagram", "http://127.0.0.1:8765")

        self.assertEqual(result["status"], "setup_required")
        self.assertIn("credentials", result["message"])

    def test_publish_uses_existing_export_and_connected_account(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            "os.environ",
            {"CLIPFARMPILOT_YOUTUBE_ACCESS_TOKEN": "test-token"},
            clear=True,
        ):
            root = Path(temporary)
            exports = root / "exports"
            exports.mkdir()
            export_id = "e" * 32
            source = exports / f"{export_id}.mp4"
            source.write_bytes(b"finished clip")
            publisher = SocialPublisher(root, exports)
            with patch.object(
                publisher,
                "_publish_youtube",
                return_value={"message": "Uploaded to YouTube.", "url": "https://youtu.be/test"},
            ) as upload:
                result = publisher.publish(export_id, ["youtube"], "Best clip", "Caption", "private")

        self.assertEqual(result["published"], 1)
        upload.assert_called_once()
        self.assertEqual(upload.call_args.args[0], source)


class DesktopSocialLinkTests(unittest.TestCase):
    def test_external_oauth_link_is_allowlisted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api = DesktopApi(root)
            with patch("desktop_launcher.webbrowser.open", return_value=True) as open_browser:
                result = api.open_external_url("https://accounts.google.com/o/oauth2/v2/auth?client_id=test")

        self.assertEqual(result["status"], "opened")
        open_browser.assert_called_once_with(
            "https://accounts.google.com/o/oauth2/v2/auth?client_id=test",
            new=2,
        )

    def test_external_link_rejects_unexpected_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            api = DesktopApi(Path(temporary))
            with self.assertRaises(ValueError):
                api.open_external_url("https://example.com/not-oauth")


if __name__ == "__main__":
    unittest.main()
