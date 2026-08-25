from __future__ import annotations

import unittest
import tempfile
import sys
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from backend.app import main
from backend.app.video import _emoji_font, _render_square_caption, generate_viral_title, safe_export_filename


class ViralTitleTests(unittest.TestCase):
    def test_late_energy_peak_generates_ending_hook_with_vod_context(self):
        energy = np.array([0.08, 0.09, 0.10, 0.12, 0.24, 0.62, 0.94], dtype=np.float32)
        with patch("backend.app.video._audio_rms_per_second", return_value=energy):
            result = generate_viral_title(
                Path("rendered.mp4"),
                source_title="FC 26 Weekend League Livestream.mp4",
                variation_seed="export-one",
            )

        self.assertEqual(result["strategy"], "source_context_big_finish")
        self.assertIn("FC 26 Weekend League", result["title"])
        self.assertEqual(result["filename"], f'{result["title"]}.mp4')

    def test_creator_caption_is_used_as_the_strongest_semantic_signal(self):
        with patch("backend.app.video._audio_rms_per_second", return_value=np.array([0.1, 0.5, 0.2])):
            result = generate_viral_title(
                Path("rendered.mp4"),
                source_title="Long stream",
                caption_text="W shave ❤️ / best reaction",
                variation_seed="caption-export",
            )

        self.assertEqual(result["strategy"], "creator_caption_escalation")
        self.assertIn("W Shave ❤️ Best Reaction", result["title"])
        self.assertNotIn("/", result["filename"])

    def test_transcript_creates_a_content_aware_title(self):
        energy = np.array([0.08, 0.09, 0.10, 0.12, 0.24, 0.62, 0.94], dtype=np.float32)
        with patch("backend.app.video._audio_rms_per_second", return_value=energy):
            result = generate_viral_title(
                Path("rendered.mp4"),
                source_title="Ranked Match Livestream.mp4",
                transcript_text="Um yeah, that was actually the craziest goal I have ever scored!",
                variation_seed="transcript-export",
            )

        self.assertEqual(result["strategy"], "transcript_big_finish")
        self.assertIn("The Craziest Goal I Have Ever Scored", result["title"])

    def test_explainer_transcript_uses_an_informative_curiosity_hook(self):
        with patch("backend.app.video._audio_rms_per_second", return_value=np.array([0.2, 0.3, 0.4])):
            result = generate_viral_title(
                Path("rendered.mp4"),
                source_title="Why Tigers Matter.mp4",
                transcript_text="Tigers are also a keystone species.",
                variation_seed="explainer-export",
            )

        self.assertEqual(result["strategy"], "transcript_explainer")
        self.assertIn("Tigers", result["title"])

    def test_repeated_exports_receive_original_recommendations(self):
        energy = np.array([0.1, 0.3, 0.8, 0.4], dtype=np.float32)
        used: set[str] = set()
        with patch("backend.app.video._audio_rms_per_second", return_value=energy):
            for index in range(25):
                result = generate_viral_title(
                    Path("rendered.mp4"),
                    transcript_text="That was the craziest goal I have ever scored!",
                    variation_seed=f"export-{index}",
                    excluded_titles=used,
                )
                self.assertNotIn(result["title"], used)
                used.add(result["title"])

        self.assertEqual(len(used), 25)

    def test_recent_title_history_persists_between_recommendations(self):
        energy = np.array([0.1, 0.3, 0.8, 0.4], dtype=np.float32)
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            main, "TITLE_HISTORY_PATH", Path(temporary) / "title-history.json"
        ), patch("backend.app.video._audio_rms_per_second", return_value=energy):
            first = main._unique_viral_title(
                Path("rendered.mp4"), "Ranked match", "", "That goal was completely impossible!", "one"
            )
            second = main._unique_viral_title(
                Path("rendered.mp4"), "Ranked match", "", "That goal was completely impossible!", "two"
            )

        self.assertNotEqual(first["title"], second["title"])

    def test_filename_removes_unsafe_characters(self):
        self.assertEqual(
            safe_export_filename('Best: clip / ever? *really*'),
            "Best clip ever really.mp4",
        )


class SquareCaptionSizeTests(unittest.TestCase):
    def test_text_and_emoji_caption_is_optically_centered(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "centered.png"
            _render_square_caption("W larp ❤️", target, 1.0)
            with Image.open(target) as image:
                bounds = image.getchannel("A").getbbox()

        self.assertIsNotNone(bounds)
        self.assertLessEqual(abs((bounds[0] + bounds[2]) / 2 - 540), 0.5)
        self.assertLessEqual(abs((bounds[1] + bounds[3]) / 2 - 540), 0.5)

    def test_caption_can_be_positioned_at_top_center_and_bottom(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bounds_by_position = {}
            for position in ("top", "center", "bottom"):
                target = root / f"{position}.png"
                _render_square_caption("W larp ❤️", target, 1.0, position)
                with Image.open(target) as image:
                    bounds_by_position[position] = image.getchannel("A").getbbox()

        top = bounds_by_position["top"]
        center = bounds_by_position["center"]
        bottom = bounds_by_position["bottom"]
        self.assertEqual(top[1], 96)
        self.assertLess(top[1], center[1])
        self.assertLess(center[1], bottom[1])
        self.assertEqual(bottom[3], 984)
        for bounds in bounds_by_position.values():
            self.assertLessEqual(abs((bounds[0] + bounds[2]) / 2 - 540), 0.5)

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
