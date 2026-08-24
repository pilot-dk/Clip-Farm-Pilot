from __future__ import annotations

import re
import unittest
from pathlib import Path


class SimpleStudioUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).parents[1] / "backend" / "app" / "static" / "index.html").read_text(
            encoding="utf-8"
        )

    def test_advanced_controls_use_progressive_disclosure(self):
        self.assertIn('<details class="section disclosure-section" id="effectsSection">', self.html)
        self.assertIn('<details class="section disclosure-section" id="publishSection">', self.html)
        self.assertNotIn('<details class="section disclosure-section" id="effectsSection" open>', self.html)
        self.assertIn("Simple Studio", self.html)

    def test_element_ids_remain_unique(self):
        identifiers = re.findall(r'\sid="([^"]+)"', self.html)
        duplicates = sorted({value for value in identifiers if identifiers.count(value) > 1})
        self.assertEqual(duplicates, [])

    def test_square_caption_export_uses_pixel_based_optical_centering(self):
        self.assertIn("function opticallyCenterCanvas(canvas, verticalPosition", self.html)
        self.assertIn("return opticallyCenterCanvas(canvas, captionPosition).toDataURL", self.html)

    def test_square_caption_has_top_center_and_bottom_placement_controls(self):
        for value, label in (("top", "Top"), ("center", "Centre"), ("bottom", "Bottom")):
            self.assertIn(f'name="captionPosition" value="{value}"', self.html)
            self.assertIn(f"<span>{label}</span>", self.html)
        self.assertIn("caption_position:", self.html)

    def test_classic_video_filters_are_available_and_exported(self):
        labels = {
            "none": "None",
            "black-white": "Black &amp; white",
            "cinematic": "Cinematic",
            "vivid": "Vivid",
            "warm": "Warm",
            "cool": "Cool",
            "faded": "Faded / Vintage",
            "high-contrast": "High contrast",
        }
        self.assertIn('<select id="videoFilter">', self.html)
        for value, label in labels.items():
            self.assertIn(f'<option value="{value}">{label}</option>', self.html)
        self.assertIn('video_filter: $("videoFilter").value', self.html)
        self.assertIn("function updateVideoFilterPreview()", self.html)

    def test_smart_sound_placement_is_enabled_with_manual_override(self):
        self.assertIn('<input id="autoSoundEffectToggle" type="checkbox" checked disabled', self.html)
        self.assertIn("likely punchline endings, reactions, and cuts", self.html)
        self.assertIn('auto_sound_effect: $("autoSoundEffectToggle").checked', self.html)
        self.assertIn("data.sound_effect_times", self.html)
        self.assertIn("smartPlacementResult", self.html)

    def test_live_captions_offer_word_highlighting_and_colour_schemes(self):
        self.assertIn('id="liveCaptionsToggle"', self.html)
        self.assertIn('id="liveCaptionScheme"', self.html)
        for value in ("pilot-lime", "ocean", "sunset", "neon-pink", "violet"):
            self.assertIn(f'value="{value}"', self.html)
        self.assertIn("live_captions:", self.html)
        self.assertIn("live_caption_scheme:", self.html)
        self.assertIn("Automatic word-by-word highlights", self.html)


if __name__ == "__main__":
    unittest.main()
