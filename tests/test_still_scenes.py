from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from backend.app import video
from backend.app.captions import CaptionWord
from backend.app.video import (
    _run,
    _still_scene_cut_intervals,
    _still_stretches_in_frames,
    export_clip,
    ffmpeg_executable,
    probe_video,
)

FPS = video._STILL_SAMPLE_FPS
HEIGHT, WIDTH = video._STILL_HEIGHT, video._STILL_WIDTH


def busy(count: int, seed: int = 1) -> list[np.ndarray]:
    """Frames where everything changes, like gameplay."""
    rng = np.random.default_rng(seed)
    return [rng.integers(0, 256, (HEIGHT, WIDTH), dtype=np.uint8) for _ in range(count)]


def still(count: int, level: int = 90, moving_corner: bool = False) -> list[np.ndarray]:
    """A frozen picture, optionally with a small face cam that keeps moving."""
    frames = []
    rng = np.random.default_rng(7)
    for _ in range(count):
        frame = np.full((HEIGHT, WIDTH), level, dtype=np.uint8)
        if moving_corner:  # 12 x 12 of 96 x 54 is under 3% of the frame.
            frame[-12:, -12:] = rng.integers(0, 256, (12, 12), dtype=np.uint8)
        frames.append(frame)
    return frames


class StillDetectionTests(unittest.TestCase):
    def test_a_frozen_stretch_between_gameplay_is_found(self):
        frames = busy(3 * FPS) + still(8 * FPS) + busy(3 * FPS, seed=2)
        stretches = _still_stretches_in_frames(frames)
        self.assertEqual(len(stretches), 1)
        start, end = stretches[0]
        self.assertAlmostEqual(start, 3.0, delta=0.3)
        self.assertAlmostEqual(end, 11.0, delta=0.3)

    def test_a_moving_face_cam_does_not_break_a_still_scene(self):
        stretches = _still_stretches_in_frames(busy(FPS) + still(10 * FPS, moving_corner=True) + busy(FPS))
        self.assertEqual(len(stretches), 1)
        self.assertGreater(stretches[0][1] - stretches[0][0], 9.0)

    def test_a_single_flash_does_not_break_it_but_a_new_scene_does(self):
        flash = still(1, level=250)
        stretches = _still_stretches_in_frames(still(4 * FPS) + flash + still(4 * FPS) + busy(FPS))
        self.assertEqual(len(stretches), 1)
        self.assertGreater(stretches[0][1] - stretches[0][0], 7.5)
        # Two scenes of four seconds each are too short, even back to back.
        self.assertEqual(_still_stretches_in_frames(still(4 * FPS, 40) + still(4 * FPS, 200) + busy(FPS)), [])

    def test_a_slow_pan_is_not_a_still_scene(self):
        gradient = np.tile(np.linspace(0, 255, WIDTH * 3).astype(np.uint8), (HEIGHT, 1))
        pan = [gradient[:, step:step + WIDTH] for step in range(0, 10 * FPS * 4, 4)]  # Drifts 1 px a frame.
        stretches = _still_stretches_in_frames(pan)
        self.assertTrue(all(end - start < 6.0 for start, end in stretches))


class StillCutTests(unittest.TestCase):
    def test_each_still_scene_keeps_a_moment_at_both_edges(self):
        cuts = _still_scene_cut_intervals([(3.0, 13.0)], [], [], None, 20.0)
        self.assertEqual(cuts, [(4.0, 12.5)])

    def test_speech_inside_a_still_scene_is_kept_with_room_around_it(self):
        cuts = _still_scene_cut_intervals([(3.0, 23.0)], [(10.0, 12.0)], [], None, 30.0)
        self.assertEqual(cuts, [(4.0, 9.7), (12.3, 22.5)])

    def test_a_word_the_speech_detector_missed_is_kept_too(self):
        levels = np.full(int(30 / video._speech_frame_seconds()), 20.0, dtype=np.float32)
        cuts = _still_scene_cut_intervals([(3.0, 23.0)], [(1.0, 2.0)], [CaptionWord("look", 15.0, 15.3)], levels, 30.0)
        self.assertEqual(sum(max(0.0, min(15.3, b) - max(15.0, a)) for a, b in cuts), 0.0)

    def test_short_leftovers_are_not_cut(self):
        # Speech at 2.2-3.8 s leaves 1.0-1.9 s and 4.1-5.0 s: both under a second.
        self.assertEqual(_still_scene_cut_intervals([(0.0, 5.5)], [(2.2, 3.8)], [], None, 10.0), [])


class StillExportTests(unittest.TestCase):
    def test_a_full_length_export_removes_a_frozen_stretch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            output = root / "edited.mp4"
            # Gameplay-like motion for 3 s, a frozen frame for 10 s, motion again for 3 s.
            _run([
                ffmpeg_executable(), "-y", "-v", "error",
                "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=3",
                "-f", "lavfi", "-i", "color=c=navy:s=320x180:r=30:d=10",
                "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=3",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=16",
                "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
                "-map", "[v]", "-map", "3:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                str(source),
            ])
            metadata: dict[str, object] = {}
            with patch.object(video, "_speech_context", return_value=(None, [])), \
                    patch.object(video, "transcribe_words", return_value=[]):
                export_clip(
                    source=source, output=output, start=0.0, end=16.0, aspect="16:9", resolution="720p",
                    edit_mode="full-length", remove_still_scenes=True, title_transcript=False,
                    auto_sound_effect=False, export_metadata=metadata,
                )

            summary = metadata["full_length_summary"]
            self.assertTrue(summary["remove_still_scenes"])
            self.assertEqual(summary["still_scenes_removed"], 1)
            # The frozen 10 s keeps 1 s at its start and 0.5 s at its end.
            self.assertAlmostEqual(summary["still_seconds_removed"], 8.5, delta=0.5)
            self.assertAlmostEqual(probe_video(output).duration, 16.0 - 8.5, delta=0.5)


if __name__ == "__main__":
    unittest.main()
