from __future__ import annotations

import dataclasses
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from backend.app import video
from backend.app.video import (
    SUBSCRIBE_ANIMATION,
    TWITCH_FOLLOW_ANIMATION,
    _has_audio,
    _intro_animation_schedule,
    _run,
    export_clip,
    ffmpeg_executable,
    probe_video,
)

RATE = 48_000


class IntroScheduleTests(unittest.TestCase):
    def test_the_bundled_animations_match_their_recorded_length_and_sound(self):
        for animation in (TWITCH_FOLLOW_ANIMATION, SUBSCRIBE_ANIMATION):
            with self.subTest(animation=animation.name):
                self.assertTrue(animation.asset.is_file())
                # The packaged apps read the length to the hundredth of a second.
                self.assertAlmostEqual(probe_video(animation.asset).duration, animation.duration, delta=0.01)
                self.assertEqual(_has_audio(animation.asset), animation.has_sound)

    def test_each_animation_starts_at_zero_on_its_own(self):
        self.assertEqual(_intro_animation_schedule(True, False, 600.0), [(TWITCH_FOLLOW_ANIMATION, 0.0)])
        self.assertEqual(_intro_animation_schedule(False, True, 600.0), [(SUBSCRIBE_ANIMATION, 0.0)])
        self.assertEqual(_intro_animation_schedule(False, False, 600.0), [])

    def test_with_both_the_twitch_animation_plays_first_and_the_subscribe_one_follows_it(self):
        self.assertEqual(
            _intro_animation_schedule(True, True, 600.0),
            [(TWITCH_FOLLOW_ANIMATION, 0.0), (SUBSCRIBE_ANIMATION, 5.7)],
        )

    def test_an_animation_that_would_start_after_the_video_ends_is_left_out(self):
        self.assertEqual(_intro_animation_schedule(True, True, 5.7), [(TWITCH_FOLLOW_ANIMATION, 0.0)])
        self.assertEqual(
            _intro_animation_schedule(True, True, 5.75),
            [(TWITCH_FOLLOW_ANIMATION, 0.0), (SUBSCRIBE_ANIMATION, 5.7)],
        )

    def test_a_missing_animation_file_is_reported_by_name(self):
        missing = dataclasses.replace(TWITCH_FOLLOW_ANIMATION, asset=Path("/nonexistent/twitch-follow.mov"))
        with patch.object(video, "TWITCH_FOLLOW_ANIMATION", missing):
            with self.assertRaisesRegex(RuntimeError, "Twitch follow animation is missing"):
                video._intro_animation_schedule(True, False, 600.0)
            self.assertEqual(video._intro_animation_schedule(False, True, 600.0), [(SUBSCRIBE_ANIMATION, 0.0)])


class IntroRenderTests(unittest.TestCase):
    """Full-length renders over a plain background with a quiet tone."""

    @classmethod
    def setUpClass(cls):
        cls._folder = tempfile.TemporaryDirectory()
        cls.root = Path(cls._folder.name)
        cls.source = cls.root / "source.mp4"
        _run([
            ffmpeg_executable(), "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=#26313d:s=640x360:r=30:d=10.5",
            "-f", "lavfi", "-i", "sine=frequency=260:sample_rate=48000:duration=10.5",
            "-af", "volume=0.05", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(cls.source),
        ])
        cls.renders: dict[tuple[str, bool, bool], tuple[Path, dict]] = {}

    @classmethod
    def tearDownClass(cls):
        cls._folder.cleanup()

    def render(self, aspect: str, twitch: bool, subscribe: bool, end: float = 10.5) -> tuple[Path, dict]:
        key = (f"{aspect}-{end}", twitch, subscribe)
        if key not in self.renders:
            output = self.root / f"{aspect.replace(':', 'x')}-{end}-{int(twitch)}{int(subscribe)}.mp4"
            metadata: dict[str, object] = {}
            export_clip(
                source=self.source, output=output, start=0.0, end=end, aspect=aspect, resolution="720p",
                edit_mode="full-length", twitch_follow_animation=twitch, subscribe_animation=subscribe,
                export_metadata=metadata,
            )
            self.renders[key] = (output, metadata["full_length_summary"])
        return self.renders[key]

    def frame(self, path: Path, second: float) -> np.ndarray:
        image = self.root / f"{path.stem}-{second:.2f}.png"
        _run([
            ffmpeg_executable(), "-y", "-v", "error", "-ss", f"{second:.3f}", "-i", str(path),
            "-frames:v", "1", str(image),
        ])
        return np.asarray(Image.open(image).convert("RGB"), dtype=np.int16)

    def change(self, path: Path, second: float, rows: slice, columns: slice) -> float:
        """How far the render differs from the plain background inside a region."""
        frame = self.frame(path, second)
        background = self.frame(self.source, second)
        background = np.asarray(
            Image.fromarray(background.astype(np.uint8)).resize((frame.shape[1], frame.shape[0])), dtype=np.int16
        )
        return float(np.abs(frame - background)[rows, columns].mean())

    def loudness(self, path: Path, start: float, end: float) -> float:
        raw = subprocess.run(
            [ffmpeg_executable(), "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", str(RATE),
             "-f", "s16le", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float64)[int(start * RATE):int(end * RATE)]
        return float(np.sqrt(np.mean(samples ** 2)))

    # On a 1280 x 720 frame the Twitch banner sits at about x 297-1023, y 528-679
    # once it has slid open, and the YouTube animation fills a band at y 157-562.
    TWITCH_BANNER = (slice(540, 670), slice(310, 1010))
    BELOW_SUBSCRIBE_BAND = (slice(580, 670), slice(310, 1010))
    SUBSCRIBE_BAND = (slice(209, 432), slice(371, 909))
    WHOLE_FRAME = (slice(None), slice(None))

    def test_the_twitch_animation_alone_starts_at_zero_and_is_gone_when_it_ends(self):
        output, summary = self.render("16:9", twitch=True, subscribe=False)

        self.assertEqual((probe_video(output).width, probe_video(output).height), (1280, 720))
        self.assertGreater(self.change(output, 0.05, slice(440, 720), slice(250, 480)), 6.0)  # The logo pops in.
        self.assertGreater(self.change(output, 2.0, *self.TWITCH_BANNER), 40.0)
        self.assertLess(self.change(output, 2.0, slice(0, 400), slice(None)), 3.0)
        self.assertLess(self.change(output, 6.0, *self.WHOLE_FRAME), 3.0)
        self.assertTrue(summary["twitch_follow_animation"])
        self.assertFalse(summary["subscribe_animation"])
        self.assertEqual(summary["intro_animations"], [{"name": "Twitch follow", "start": 0.0, "end": 5.7}])
        # It has no sound of its own, so the video's sound is left exactly as it was.
        self.assertAlmostEqual(self.loudness(output, 0.2, 5.5), self.loudness(self.source, 0.2, 5.5), delta=60)

    def test_with_both_the_subscribe_animation_and_its_sound_follow_the_twitch_animation(self):
        output, summary = self.render("16:9", twitch=True, subscribe=True)

        # 0-5.7 s: the Twitch banner, and no subscribe animation yet.
        self.assertGreater(self.change(output, 2.0, *self.TWITCH_BANNER), 40.0)
        self.assertLess(self.change(output, 2.0, *self.SUBSCRIBE_BAND), 3.0)
        # 5.7-9.417 s: the subscribe animation, with the Twitch banner gone.
        self.assertGreater(self.change(output, 6.8, *self.SUBSCRIBE_BAND), 9.0)
        self.assertLess(self.change(output, 6.8, *self.BELOW_SUBSCRIBE_BAND), 3.0)
        # Afterwards, only the video.
        self.assertLess(self.change(output, 9.9, *self.WHOLE_FRAME), 3.0)
        self.assertTrue(summary["twitch_follow_animation"])
        self.assertTrue(summary["subscribe_animation"])
        self.assertEqual(
            summary["intro_animations"],
            [
                {"name": "Twitch follow", "start": 0.0, "end": 5.7},
                {"name": "YouTube subscribe", "start": 5.7, "end": 9.417},
            ],
        )
        # The subscribe sound plays with its animation, not over the Twitch one.
        source_early, source_late = self.loudness(self.source, 0.2, 5.5), self.loudness(self.source, 5.9, 8.5)
        self.assertAlmostEqual(self.loudness(output, 0.2, 5.5), source_early, delta=60)
        self.assertGreater(self.loudness(output, 5.9, 8.5), source_late * 1.3)
        self.assertAlmostEqual(self.loudness(output, 9.6, 10.3), self.loudness(self.source, 9.6, 10.3), delta=60)

    def test_the_subscribe_animation_alone_still_starts_at_zero(self):
        output, summary = self.render("16:9", twitch=False, subscribe=True)

        self.assertGreater(self.change(output, 1.1, *self.SUBSCRIBE_BAND), 9.0)
        self.assertLess(self.change(output, 1.1, *self.BELOW_SUBSCRIBE_BAND), 3.0)
        self.assertLess(self.change(output, 4.2, *self.WHOLE_FRAME), 3.0)
        self.assertEqual(summary["intro_animations"], [{"name": "YouTube subscribe", "start": 0.0, "end": 3.717}])
        self.assertGreater(self.loudness(output, 0.2, 2.8), self.loudness(self.source, 0.2, 2.8) * 1.3)

    def test_a_square_video_shows_the_whole_twitch_animation_letterboxed(self):
        output, summary = self.render("1:1", twitch=True, subscribe=False)

        # 1920 x 1080 fitted to 720 x 405 and centred, so the banner lands at y 454-539.
        self.assertEqual((probe_video(output).width, probe_video(output).height), (720, 720))
        self.assertGreater(self.change(output, 2.0, slice(462, 532), slice(180, 560)), 40.0)
        self.assertLess(self.change(output, 2.0, slice(0, 400), slice(None)), 3.0)
        self.assertLess(self.change(output, 6.0, *self.WHOLE_FRAME), 3.0)
        self.assertTrue(summary["twitch_follow_animation"])

    def test_a_video_too_short_for_the_second_animation_shows_only_the_first(self):
        output, summary = self.render("16:9", twitch=True, subscribe=True, end=4.0)

        self.assertAlmostEqual(probe_video(output).duration, 4.0, delta=0.1)
        self.assertGreater(self.change(output, 2.0, *self.TWITCH_BANNER), 40.0)
        self.assertTrue(summary["twitch_follow_animation"])
        self.assertFalse(summary["subscribe_animation"])
        self.assertEqual(summary["intro_animations"], [{"name": "Twitch follow", "start": 0.0, "end": 4.0}])


if __name__ == "__main__":
    unittest.main()
