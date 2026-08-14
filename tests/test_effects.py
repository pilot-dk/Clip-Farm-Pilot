from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np
from PIL import Image

from backend.app.video import _render_sound_effect, _render_visual_overlay


class EffectAssetTests(unittest.TestCase):
    def test_original_effect_sounds_are_valid_and_audible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for effect in ("impact-boom", "whoosh", "record-scratch"):
                target = root / f"{effect}.wav"
                _render_sound_effect(effect, target)
                with wave.open(str(target), "rb") as audio:
                    samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype=np.int16)
                    self.assertEqual(audio.getframerate(), 48_000)
                    self.assertEqual(audio.getnchannels(), 1)
                self.assertGreater(samples.size, 20_000)
                self.assertGreater(int(np.max(np.abs(samples))), 1_000)

    def test_lens_flare_overlay_contains_transparent_and_visible_pixels(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "lens-flare.png"
            _render_visual_overlay("lens-flare", target, 1080, 1080, 1.0)
            alpha = np.asarray(Image.open(target).convert("RGBA"))[:, :, 3]

        self.assertEqual(alpha.shape, (1080, 1080))
        self.assertEqual(int(alpha.min()), 0)
        self.assertGreater(int(alpha.max()), 180)

    def test_white_flash_strength_changes_opacity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            weak = root / "weak.png"
            strong = root / "strong.png"
            _render_visual_overlay("white-flash", weak, 64, 64, 0.25)
            _render_visual_overlay("white-flash", strong, 64, 64, 1.0)
            weak_alpha = Image.open(weak).convert("RGBA").getpixel((0, 0))[3]
            strong_alpha = Image.open(strong).convert("RGBA").getpixel((0, 0))[3]

        self.assertLess(weak_alpha, strong_alpha)


if __name__ == "__main__":
    unittest.main()
