from __future__ import annotations

import tempfile
import unittest
import wave
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

from backend.app.video import (
    VIDEO_FILTER_CHAINS,
    _apply_effects,
    _run,
    _smart_sound_times_from_signals,
    _video_filter_chain,
    ffmpeg_executable,
    _render_sound_effect,
    _render_visual_overlay,
)


class EffectAssetTests(unittest.TestCase):
    @staticmethod
    def _decoded_mono_audio(path: Path) -> np.ndarray:
        result = subprocess.run(
            [
                ffmpeg_executable(), "-v", "error", "-i", str(path),
                "-map", "0:a:0", "-ac", "1", "-ar", "48000",
                "-f", "s16le", "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return np.frombuffer(result.stdout, dtype=np.int16)

    def test_vine_boom_smart_placement_can_select_multiple_phrase_endings(self):
        envelope = np.full(240, 0.02, dtype=np.float32)
        for start, stop in ((10, 25), (72, 88), (145, 164), (205, 220)):
            envelope[start:stop] = np.linspace(0.45, 0.92, stop - start)
        times = _smart_sound_times_from_signals(
            envelope,
            hop_seconds=0.10,
            duration=24.0,
            sound_effect="vine-boom",
            fallback_time=1.0,
        )

        self.assertGreaterEqual(len(times), 3)
        self.assertEqual(times, sorted(times))
        self.assertTrue(all(b - a >= 3.2 for a, b in zip(times, times[1:])))
        self.assertTrue(any(abs(value - 8.8) < 0.35 for value in times))
        self.assertTrue(any(abs(value - 16.4) < 0.35 for value in times))

    def test_smart_fallback_leaves_an_audible_tail(self):
        times = _smart_sound_times_from_signals(
            np.zeros(24, dtype=np.float32),
            hop_seconds=0.10,
            duration=2.4,
            sound_effect="vine-boom",
            fallback_time=99.0,
        )

        self.assertEqual(times, [1.8])

    def test_repeated_sound_effects_mix_into_one_finished_clip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            target = root / "repeated.mp4"
            _run([
                ffmpeg_executable(), "-y", "-v", "error",
                "-f", "lavfi", "-i", "color=c=navy:s=96x64:r=24:d=2.4",
                "-f", "lavfi", "-i", "sine=frequency=260:sample_rate=48000:duration=2.4",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source),
            ])

            _apply_effects(
                source,
                target,
                duration=2.4,
                width=96,
                height=64,
                sound_effect="vine-boom",
                visual_effect="none",
                effect_time=0.5,
                sound_effect_times=[0.35, 1.35],
                sound_volume=0.75,
                visual_strength=1.0,
            )

            self.assertTrue(target.is_file())
            self.assertGreater(target.stat().st_size, source.stat().st_size // 2)

    def test_vine_boom_is_audible_in_the_finished_mp4_near_the_clip_end(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            baseline = root / "baseline.mp4"
            target = root / "vine.mp4"
            _run([
                ffmpeg_executable(), "-y", "-v", "error",
                "-f", "lavfi", "-i", "color=c=navy:s=96x64:r=24:d=2.4",
                "-f", "lavfi", "-i", "sine=frequency=260:sample_rate=48000:duration=2.4",
                "-af", "volume=0.8",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source),
            ])

            for output, sound in ((baseline, "none"), (target, "vine-boom")):
                _apply_effects(
                    source,
                    output,
                    duration=2.4,
                    width=96,
                    height=64,
                    sound_effect=sound,
                    visual_effect="none",
                    effect_time=2.15,
                    sound_effect_times=[2.15],
                    sound_volume=1.0,
                    visual_strength=1.0,
                )

            baseline_audio = self._decoded_mono_audio(baseline)
            effected_audio = self._decoded_mono_audio(target)
            start, stop = round(2.16 * 48_000), round(2.36 * 48_000)
            baseline_rms = float(np.sqrt(np.mean(baseline_audio[start:stop].astype(np.float64) ** 2)))
            effected_rms = float(np.sqrt(np.mean(effected_audio[start:stop].astype(np.float64) ** 2)))

            self.assertGreater(effected_rms, baseline_rms * 2.0)
            self.assertGreater(int(np.max(np.abs(effected_audio[start:stop].astype(np.int32)))), 12_000)

    def test_zero_effect_volume_does_not_duck_source_audio(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            target = root / "muted-effect.mp4"
            _run([
                ffmpeg_executable(), "-y", "-v", "error",
                "-f", "lavfi", "-i", "color=c=navy:s=96x64:r=24:d=2.0",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2.0",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source),
            ])
            _apply_effects(
                source,
                target,
                duration=2.0,
                width=96,
                height=64,
                sound_effect="vine-boom",
                visual_effect="none",
                effect_time=0.7,
                sound_effect_times=[0.7],
                sound_volume=0.0,
                visual_strength=1.0,
            )

            source_audio = self._decoded_mono_audio(source)
            effected_audio = self._decoded_mono_audio(target)
            start, stop = round(0.75 * 48_000), round(1.40 * 48_000)
            source_rms = float(np.sqrt(np.mean(source_audio[start:stop].astype(np.float64) ** 2)))
            effected_rms = float(np.sqrt(np.mean(effected_audio[start:stop].astype(np.float64) ** 2)))
            self.assertGreater(effected_rms, source_rms * 0.90)

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

    def test_removed_sound_effects_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "removed.wav"
            for effect in ("impact-boom", "whoosh", "record-scratch"):
                with self.assertRaisesRegex(ValueError, "Unknown sound effect"):
                    _render_sound_effect(effect, target)

    def test_vine_boom_sample_is_valid_stereo_audio(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "vine-boom.wav"
            _render_sound_effect("vine-boom", target)
            with wave.open(str(target), "rb") as audio:
                samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype=np.int16)
                self.assertEqual(audio.getframerate(), 48_000)
                self.assertEqual(audio.getnchannels(), 2)
                self.assertGreater(audio.getnframes(), 120_000)

        self.assertGreater(int(np.max(np.abs(samples))), 1_000)
        self.assertLessEqual(int(np.max(np.abs(samples[:2]))), 100)
        first_50_ms = samples[: round(0.05 * 48_000) * 2]
        self.assertGreater(int(np.max(np.abs(first_50_ms))), 1_000)

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
