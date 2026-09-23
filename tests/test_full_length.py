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
    _remap_transcript_after_cuts,
    _render_kept_segments,
    _run,
    _transcribe_words_cached,
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

    def test_transcript_timestamps_follow_removed_sections(self):
        words = [
            CaptionWord("first", 0.10, 0.35),
            CaptionWord("um", 1.10, 1.35),
            CaptionWord("second", 2.10, 2.45),
        ]

        remapped = _remap_transcript_after_cuts(words, [(0.90, 1.60)], 3.0)

        self.assertEqual([word.text for word in remapped], ["first", "second"])
        self.assertAlmostEqual(remapped[1].start, 1.40, places=2)
        self.assertAlmostEqual(remapped[1].end, 1.75, places=2)

    def test_repeated_edit_reuses_the_local_transcript(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.mp4"
            self._make_pause_video(source, duration=2.0)
            transcript = [CaptionWord("hello", 0.10, 0.40)]

            with patch("backend.app.video.transcribe_words", return_value=transcript) as transcribe:
                first = _transcribe_words_cached(source, 0.0, 2.0)
                second = _transcribe_words_cached(source, 0.0, 2.0)

            self.assertEqual(first, second)
            transcribe.assert_called_once()

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
                CaptionWord("um", 2.84, 3.06),
                CaptionWord("great", 3.18, 3.42),
            ]

            with (
                patch("backend.app.video.transcribe_words", return_value=transcript) as transcribe,
                patch("backend.app.video._run", wraps=_run) as run_command,
            ):
                placements = export_clip(
                    source=source,
                    output=output,
                    start=0.0,
                    end=4.0,
                    aspect="16:9",
                    edit_mode="full-length",
                    remove_silence=True,
                    remove_filler_words=True,
                    sound_effects=["vine-boom", "check-sound"],
                    auto_sound_effect=True,
                    video_filter="cinematic",
                    live_captions=True,
                    title_transcript=True,
                    subscribe_animation=True,
                    export_metadata=metadata,
                )

            transcribe.assert_called_once()
            video_encodes = [
                call.args[0] for call in run_command.call_args_list
                if "-c:v" in call.args[0]
            ]
            self.assertEqual(len(video_encodes), 1)
            self.assertEqual(set(placements), {"vine-boom", "check-sound"})
            self.assertTrue(all(placements.values()))
            self.assertTrue(output.is_file())
            self.assertEqual((probe_video(output).width, probe_video(output).height), (1920, 1080))
            summary = metadata["full_length_summary"]
            self.assertGreater(summary["removed_seconds"], 0.5)
            self.assertTrue(summary["remove_silence"])
            self.assertTrue(summary["remove_filler_words"])
            self.assertEqual(summary["filler_words_removed"], 1)
            self.assertEqual(summary["render_passes"], 1)
            self.assertTrue(summary["shared_transcript"])
            self.assertEqual(summary["quality"], "single-pass-crf18")
            self.assertNotIn("um", metadata["title_transcript"].split())
            self.assertGreater(metadata["live_caption_word_count"], 0)

    def test_full_length_square_export_keeps_square_captions_and_full_frame_effects(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            output = root / "full-square.mp4"
            early_frame = root / "early.png"
            late_frame = root / "late.png"
            metadata: dict[str, object] = {}
            self._make_pause_video(source)
            transcript = [CaptionWord("hello", 0.10, 0.42)]

            with patch("backend.app.video.transcribe_words", return_value=transcript):
                export_clip(
                    source=source,
                    output=output,
                    start=0.0,
                    end=4.0,
                    aspect="1:1",
                    edit_mode="full-length",
                    caption_text="SQUARE CAPTION ❤️",
                    caption_position="bottom",
                    video_filter="warm",
                    visual_effect="lens-flare",
                    effect_time=0.70,
                    live_captions=True,
                    title_transcript=True,
                    subscribe_animation=True,
                    export_metadata=metadata,
                )

            self.assertTrue(output.is_file())
            self.assertEqual((probe_video(output).width, probe_video(output).height), (1080, 1080))
            summary = metadata["full_length_summary"]
            self.assertEqual(summary["aspect"], "1:1")
            self.assertEqual((summary["width"], summary["height"]), (1080, 1080))
            self.assertTrue(summary["square_caption"])
            self.assertTrue(summary["subscribe_animation"])
            self.assertGreater(metadata["live_caption_word_count"], 0)

            early = self._frame(output, 1.10, early_frame)
            late = self._frame(output, 3.85, late_frame)
            # The subscribe animation is letterboxed onto the square canvas,
            # while the creator's square caption remains after it finishes.
            self.assertGreater(int(np.max(early[220:860, 120:960])), 180)
            self.assertGreater(int(np.max(late[820:1020, 80:1000])), 220)

    # Three 2-second parts, like a stream that switches 180p mono -> 270p stereo
    # -> 180p mono. Each part has its own colour and tone to check what landed where.
    SWITCH_PARTS = (("320x180", 1, "red", 330), ("480x270", 2, "lime", 550), ("320x180", 1, "blue", 880))

    @classmethod
    def _make_switching_video(cls, root: Path) -> Path:
        # MPEG-TS carries its codec settings in-band, so joining the parts byte for
        # byte gives one file whose frame size and channel count change mid-stream.
        pieces = []
        for index, (size, channels, colour, frequency) in enumerate(cls.SWITCH_PARTS):
            part = root / f"part{index}.ts"
            _run([
                ffmpeg_executable(), "-y", "-v", "error",
                "-f", "lavfi", "-i", f"color=c={colour}:s={size}:r=30:d=2",
                "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=48000:duration=2",
                "-ac", str(channels), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-output_ts_offset", str(2 * index), "-f", "mpegts", str(part),
            ])
            pieces.append(part.read_bytes())
        source = root / "switching.ts"
        source.write_bytes(b"".join(pieces))
        return source

    @staticmethod
    def _dominant_frequency(samples: np.ndarray) -> float:
        spectrum = np.abs(np.fft.rfft(samples * np.hanning(samples.size)))
        return float(np.argmax(spectrum) * 48_000 / samples.size)

    def test_full_length_edit_survives_resolution_and_channel_switches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._make_switching_video(root)
            duration = probe_video(source).duration
            # Keep 0-1.5 (red), 2.5-3.5 (lime) and 4.5-end (blue): every join
            # crosses a switch in frame size and channel count.
            for cuts, expected_duration, checkpoints in (
                ([(1.5, 2.5), (3.5, 4.5)], duration - 2.0, ((0.75, 0), (2.0, 1), (3.2, 2))),
                ([], duration, ((1.0, 0), (3.0, 1), (5.0, 2))),
            ):
                output = root / f"edited-{len(cuts)}.mp4"
                with patch("backend.app.video._speech_pause_cut_intervals", return_value=cuts):
                    export_clip(
                        source=source,
                        output=output,
                        start=0.0,
                        end=duration,
                        aspect="16:9",
                        resolution="720p",
                        edit_mode="full-length",
                        remove_silence=True,
                        title_transcript=False,
                        auto_sound_effect=False,
                    )

                info = probe_video(output)
                self.assertEqual((info.width, info.height), (1280, 720))
                self.assertAlmostEqual(info.duration, expected_duration, delta=0.15)
                audio = self._audio(output).astype(np.float64)
                for second, part in checkpoints:
                    pixel = self._frame(output, second, root / "frame.png")[360, 640]
                    self.assertEqual(int(np.argmax(pixel)), part, f"{cuts}: wrong picture at {second}s")
                    window = audio[int((second - 0.2) * 48_000): int((second + 0.2) * 48_000)]
                    self.assertAlmostEqual(
                        self._dominant_frequency(window), self.SWITCH_PARTS[part][3], delta=15,
                        msg=f"{cuts}: sound out of step with the picture at {second}s",
                    )

    def test_legacy_segment_render_survives_resolution_and_channel_switches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._make_switching_video(root)
            edited = root / "edited.mp4"

            kept = _render_kept_segments(source, edited, 0.0, [(0.0, 1.5), (2.5, 3.5), (4.5, 5.9)])

            info = probe_video(edited)
            self.assertEqual((info.width, info.height), (320, 180))
            self.assertAlmostEqual(info.duration, kept, delta=0.15)

    def test_full_length_vertical_layout_remains_rejected(self):
        with self.assertRaisesRegex(ValueError, "16:9 or 1:1"):
            export_clip(
                source=Path("unused.mp4"),
                output=Path("unused-output.mp4"),
                start=0.0,
                end=1.0,
                aspect="9:16",
                edit_mode="full-length",
            )


if __name__ == "__main__":
    unittest.main()
