from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from backend.app.captions import CaptionWord
from backend.app.video import (
    _apply_subscribe_animation,
    _filler_word_cut_intervals,
    _prepare_full_length_source,
    _run,
    export_clip,
    ffmpeg_executable,
    probe_video,
)


class FullLengthEditorTests(unittest.TestCase):
    @staticmethod
    def _make_pause_video(path: Path, duration: float = 4.0) -> None:
        _run([
            ffmpeg_executable(), "-y", "-v", "error",
            "-f", "lavfi", "-i", f"color=c=navy:s=320x180:r=30:d={duration}",
            "-f", "lavfi", "-i",
            f"aevalsrc=if(between(t\\,1\\,2)\\,0\\,0.08*sin(2*PI*440*t)):s=48000:d={duration}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
        ])

    @staticmethod
    def _frame(path: Path, second: float, output: Path) -> np.ndarray:
        _run([
            ffmpeg_executable(), "-y", "-v", "error", "-ss", f"{second:.3f}",
            "-i", str(path), "-frames:v", "1", str(output),
        ])
        return np.asarray(Image.open(output).convert("RGB"), dtype=np.int16)

    @staticmethod
    def _audio(path: Path) -> np.ndarray:
        result = subprocess.run(
            [
                ffmpeg_executable(), "-v", "error", "-i", str(path), "-map", "0:a:0",
                "-ac", "1", "-ar", "48000", "-f", "s16le", "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return np.frombuffer(result.stdout, dtype=np.int16)

    def test_filler_words_and_phrases_become_conservative_cuts(self):
        words = [
            CaptionWord("We", 0.00, 0.20),
            CaptionWord("um", 0.45, 0.72),
            CaptionWord("did", 0.90, 1.10),
            CaptionWord("it", 1.10, 1.30),
            CaptionWord("you", 1.70, 1.90),
            CaptionWord("know", 1.90, 2.15),
            CaptionWord("today", 2.30, 2.60),
        ]

        intervals, count = _filler_word_cut_intervals(words, 3.0)

        self.assertEqual(count, 3)
        self.assertEqual(len(intervals), 2)
        self.assertLess(intervals[0][0], words[1].start)
        self.assertGreater(intervals[1][1], words[5].end)

    def test_silence_cleanup_shortens_a_pause_without_losing_the_tones(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            edited = root / "edited.mp4"
            self._make_pause_video(source)

            summary = _prepare_full_length_source(source, edited, 0.0, 4.0, True, False)

            self.assertTrue(edited.is_file())
            self.assertGreaterEqual(summary["silence_sections_removed"], 1)
            self.assertGreater(summary["removed_seconds"], 0.5)
            self.assertLess(probe_video(edited).duration, 3.5)
            audio = self._audio(edited).astype(np.int32)
            self.assertGreater(int(np.max(np.abs(audio[:24_000]))), 1_000)
            self.assertGreater(int(np.max(np.abs(audio[-24_000:]))), 1_000)

    def test_subscribe_animation_is_complete_at_start_and_gone_after_it_finishes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            output = root / "subscribed.mp4"
            source_early = root / "source-early.png"
            output_early = root / "output-early.png"
            source_late = root / "source-late.png"
            output_late = root / "output-late.png"
            _run([
                ffmpeg_executable(), "-y", "-v", "error",
                "-f", "lavfi", "-i", "color=c=#26313d:s=320x180:r=30:d=4.4",
                "-f", "lavfi", "-i", "sine=frequency=260:sample_rate=48000:duration=4.4",
                "-af", "volume=0.05", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest", str(source),
            ])

            _apply_subscribe_animation(source, output)

            early_difference = np.abs(
                self._frame(output, 1.1, output_early) - self._frame(source, 1.1, source_early)
            )
            late_difference = np.abs(
                self._frame(output, 4.05, output_late) - self._frame(source, 4.05, source_late)
            )
            self.assertEqual((probe_video(output).width, probe_video(output).height), (320, 180))
            subscribe_region = early_difference[52:132, 92:228]
            self.assertGreater(float(subscribe_region.mean()), 9.0)
            self.assertGreater(float(np.percentile(subscribe_region, 99)), 40.0)
            self.assertLess(float(late_difference.mean()), 4.0)
            source_audio = self._audio(source).astype(np.int32)
            output_audio = self._audio(output).astype(np.int32)
            start, end = round(0.2 * 48_000), round(2.8 * 48_000)
            self.assertGreater(
                float(np.sqrt(np.mean(output_audio[start:end].astype(np.float64) ** 2))),
                float(np.sqrt(np.mean(source_audio[start:end].astype(np.float64) ** 2))) * 1.3,
            )

    def test_full_length_export_keeps_smart_effects_on_the_cleaned_timeline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            output = root / "full-youtube.mp4"
            metadata: dict[str, object] = {}
            self._make_pause_video(source)
            transcript = [
                CaptionWord("that", 0.10, 0.30),
                CaptionWord("was", 0.30, 0.48),
                CaptionWord("weird", 0.48, 0.72),
                CaptionWord("we", 2.10, 2.28),
                CaptionWord("did", 2.28, 2.46),
                CaptionWord("it", 2.46, 2.70),
            ]

            with patch("backend.app.video.transcribe_words", return_value=transcript):
                placements = export_clip(
                    source=source,
                    output=output,
                    start=0.0,
                    end=4.0,
                    aspect="16:9",
                    edit_mode="full-length",
                    remove_silence=True,
                    sound_effects=["vine-boom", "check-sound"],
                    auto_sound_effect=True,
                    video_filter="cinematic",
                    export_metadata=metadata,
                )

            self.assertEqual(set(placements), {"vine-boom", "check-sound"})
            self.assertTrue(all(placements.values()))
            self.assertTrue(output.is_file())
            self.assertEqual((probe_video(output).width, probe_video(output).height), (1920, 1080))
            summary = metadata["full_length_summary"]
            self.assertGreater(summary["removed_seconds"], 0.5)
            self.assertTrue(summary["remove_silence"])


if __name__ == "__main__":
    unittest.main()
