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


if __name__ == "__main__":
    unittest.main()
