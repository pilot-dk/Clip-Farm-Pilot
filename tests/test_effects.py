from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np
from PIL import Image

from backend.app.video import (
    VIDEO_FILTER_CHAINS,
    _run,
    _video_filter_chain,
    ffmpeg_executable,
    _render_sound_effect,
    _render_visual_overlay,
)


class EffectAssetTests(unittest.TestCase):
    def test_classic_video_filter_chains_are_defined(self):
        self.assertEqual(_video_filter_chain("none"), "")
        for name in (
            "black-white",
            "cinematic",
            "vivid",
            "warm",
            "cool",
            "faded",
            "high-contrast",
        ):
            self.assertTrue(_video_filter_chain(name))
        with self.assertRaisesRegex(ValueError, "Unknown video filter"):
            _video_filter_chain("not-a-filter")

    def test_every_classic_video_filter_renders_a_changed_frame(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.bmp"
            x = np.linspace(0, 255, 96, dtype=np.uint8)
            y = np.linspace(0, 255, 64, dtype=np.uint8)[:, None]
            pixels = np.zeros((64, 96, 3), dtype=np.uint8)
            pixels[:, :, 0] = x
            pixels[:, :, 1] = y
            pixels[:, :, 2] = 220 - (x // 2)
            Image.fromarray(pixels, "RGB").save(source)

            for name in VIDEO_FILTER_CHAINS:
                if name == "none":
                    continue
                target = root / f"{name}.bmp"
                _run([
                    ffmpeg_executable(), "-y", "-v", "error", "-i", str(source),
                    "-vf", _video_filter_chain(name), "-frames:v", "1", str(target),
                ])
                rendered = np.asarray(Image.open(target).convert("RGB"), dtype=np.int16)
                difference = np.abs(rendered - pixels.astype(np.int16))
                self.assertGreater(float(difference.mean()), 1.0, name)
                if name == "black-white":
                    self.assertLess(float(np.abs(rendered[:, :, 0] - rendered[:, :, 1]).mean()), 1.0)
                    self.assertLess(float(np.abs(rendered[:, :, 1] - rendered[:, :, 2]).mean()), 1.0)

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

    def test_vine_boom_sample_is_valid_stereo_audio(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "vine-boom.wav"
            _render_sound_effect("vine-boom", target)
            with wave.open(str(target), "rb") as audio:
                samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype=np.int16)
                self.assertEqual(audio.getframerate(), 48_000)
                self.assertEqual(audio.getnchannels(), 2)
                self.assertGreater(audio.getnframes(), 150_000)

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
