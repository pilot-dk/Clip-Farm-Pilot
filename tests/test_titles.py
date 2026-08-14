from __future__ import annotations

import unittest
import tempfile
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from backend.app.video import _emoji_font, _render_square_caption, generate_viral_title, safe_export_filename


class ViralTitleTests(unittest.TestCase):
    def test_late_energy_peak_generates_ending_hook_with_vod_context(self):
        energy = np.array([0.08, 0.09, 0.10, 0.12, 0.24, 0.62, 0.94], dtype=np.float32)
        with patch("backend.app.video._audio_rms_per_second", return_value=energy):
            result = generate_viral_title(
                Path("rendered.mp4"),
                source_title="FC 26 Weekend League Livestream.mp4",
            )

        self.assertEqual(result["strategy"], "big_finish")
        self.assertEqual(result["title"], "FC 26 Weekend League — Wait for the Ending")
        self.assertEqual(result["filename"], "FC 26 Weekend League — Wait for the Ending.mp4")

    def test_creator_caption_is_used_as_the_strongest_semantic_signal(self):
        with patch("backend.app.video._audio_rms_per_second", return_value=np.array([0.1, 0.5, 0.2])):
            result = generate_viral_title(
                Path("rendered.mp4"),
                source_title="Long stream",
                caption_text="W shave ❤️ / best reaction",
            )

        self.assertEqual(result["strategy"], "creator_caption")
        self.assertEqual(result["title"], "W shave ❤️ / best reaction")
        self.assertEqual(result["filename"], "W shave ❤️ best reaction.mp4")

    def test_filename_removes_unsafe_characters(self):
        self.assertEqual(
            safe_export_filename('Best: clip / ever? *really*'),
            "Best clip ever really.mp4",
        )


class SquareCaptionSizeTests(unittest.TestCase):
    def test_caption_scale_changes_rendered_text_size(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            small_path = root / "small.png"
            large_path = root / "large.png"
            _render_square_caption("Caption test", small_path, 0.50)
            _render_square_caption("Caption test", large_path, 1.50)

            with Image.open(small_path) as small_image, Image.open(large_path) as large_image:
                small_box = small_image.getchannel("A").getbbox()
                large_box = large_image.getchannel("A").getbbox()

            self.assertIsNotNone(small_box)
            self.assertIsNotNone(large_box)
            self.assertGreater(large_box[2] - large_box[0], (small_box[2] - small_box[0]) * 2)
            self.assertGreater(large_box[3] - large_box[1], (small_box[3] - small_box[1]) * 2)

    @unittest.skipUnless(sys.platform == "darwin", "Uses the native macOS color-emoji renderer")
    def test_color_emoji_render_at_normal_caption_size_without_missing_glyph_boxes(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "emoji.png"
            _render_square_caption("❤️ 👍🏽 👨‍👩‍👧‍👦 🇨🇦", target, 1.0)
            pixels = np.asarray(Image.open(target).convert("RGBA"))

        visible = pixels[:, :, 3] > 100
        colorful = (
            (pixels[:, :, :3].max(axis=2) - pixels[:, :, :3].min(axis=2) > 35)
            & visible
        )
        self.assertGreater(int(colorful.sum()), 5_000)
        self.assertIsNotNone(_emoji_font(86))


if __name__ == "__main__":
    unittest.main()
