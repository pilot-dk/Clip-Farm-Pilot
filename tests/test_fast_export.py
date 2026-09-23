from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

import numpy as np

from backend.app import video
from backend.app.video import (
    _copy_kept_audio,
    _encode_video,
    _filter_graph_args,
    _gaps_between,
    _hardware_encoder,
    _kept_frames_filter,
    _run,
    _run_with_kept_audio,
    export_clip,
    ffmpeg_executable,
    probe_video,
)


def _stereo_ramp(frames: int) -> bytes:
    """Stereo PCM whose left sample is its own index (mod 20000) and right is its negative."""
    left = (np.arange(frames) % 20_000).astype("<i2")
    return np.column_stack([left, -left]).astype("<i2").tobytes()


class KeptAudioTests(unittest.TestCase):
    def _copy(self, frames: int, segments: list[tuple[float, float]]) -> tuple[np.ndarray, bool]:
        output = io.BytesIO()
        stopped_early = _copy_kept_audio(io.BytesIO(_stereo_ramp(frames)), output, segments)
        return np.frombuffer(output.getvalue(), dtype="<i2").reshape(-1, 2), stopped_early

    def test_keeps_exactly_the_segments_with_short_fades_at_each_join(self):
        # Segments that straddle the one-second read blocks.
        segments = [(0.5, 1.5), (2.25, 2.75)]
        kept, stopped_early = self._copy(48_000 * 3, segments)

        self.assertEqual(len(kept), 48_000 + 24_000)
        self.assertTrue(stopped_early)
        fade = round(0.012 * 48_000)
        # Away from the joins every sample is copied untouched, from the right place.
        np.testing.assert_array_equal(kept[fade:48_000 - fade, 0], np.arange(24_000 + fade, 72_000 - fade) % 20_000)
        np.testing.assert_array_equal(kept[48_000 + fade:-fade, 0], np.arange(108_000 + fade, 132_000 - fade) % 20_000)
        np.testing.assert_array_equal(kept[:, 1], -kept[:, 0])
        # The join fades out to near silence and back in.
        self.assertLess(abs(int(kept[48_000 - 1, 0])), 40)
        self.assertLess(abs(int(kept[48_000, 0])), 40)
        self.assertGreater(abs(int(kept[48_000 + fade, 0])), 1_000)

    def test_a_stream_shorter_than_the_segments_ends_cleanly(self):
        kept, stopped_early = self._copy(48_000, [(0.5, 2.0)])

        self.assertEqual(len(kept), 24_000)
        self.assertFalse(stopped_early)


class KeptFramesFilterTests(unittest.TestCase):
    def test_an_uncut_selection_only_restarts_the_clock(self):
        self.assertEqual(_kept_frames_filter([(0.0, 12.0)], 12.0), "setpts=PTS-STARTPTS")

    def test_each_kept_frame_moves_back_by_everything_removed_before_it(self):
        chain = _kept_frames_filter([(1.0, 2.0), (3.5, 4.0)], 5.0)

        self.assertIn("gte(t,1.0000)*lt(t,2.0000)+gte(t,3.5000)*lt(t,4.0000)", chain)
        self.assertIn("setpts='(T-(1.0000*gte(T,1.0000)+1.5000*gte(T,3.5000)))/TB'", chain)

    def test_gaps_are_the_complement_of_the_kept_segments(self):
        self.assertEqual(_gaps_between([(1.0, 2.0), (3.5, 4.0)], 5.0), [(0.0, 1.0), (2.0, 3.5), (4.0, 5.0)])
        self.assertEqual(_gaps_between([(0.0, 5.0)], 5.0), [])


class FilterGraphArgumentTests(unittest.TestCase):
    def test_short_graphs_stay_on_the_command_line(self):
        temporary: list[Path] = []
        self.assertEqual(_filter_graph_args("ffmpeg", "[0:v]null[v]", temporary), ["-filter_complex", "[0:v]null[v]"])
        self.assertEqual(temporary, [])

    def test_long_graphs_go_through_a_file_in_the_syntax_the_ffmpeg_understands(self):
        graph = "[0:v]" + ",".join(["null"] * 3_000) + "[v]"
        for version, option in ((7, "-/filter_complex"), (6, "-filter_complex_script")):
            temporary: list[Path] = []
            with patch("backend.app.video._ffmpeg_major_version", return_value=version):
                args = _filter_graph_args("ffmpeg", graph, temporary)
            try:
                self.assertEqual(args[0], option)
                self.assertEqual(Path(args[1]).read_text(encoding="utf-8"), graph)
            finally:
                for path in temporary:
                    path.unlink(missing_ok=True)


class EncoderChoiceTests(unittest.TestCase):
    def setUp(self):
        video._HARDWARE_ENCODER_CHOICES.clear()
        self.addCleanup(video._HARDWARE_ENCODER_CHOICES.clear)

    def test_the_first_hardware_encoder_that_works_is_chosen_once(self):
        with (
            patch("backend.app.video.sys.platform", "win32"),
            patch("backend.app.video._encoder_works", side_effect=[False, True]) as works,
        ):
            first = _hardware_encoder("ffmpeg")
            second = _hardware_encoder("ffmpeg")

        self.assertEqual(first[0], "Quick Sync")
        self.assertIs(first, second)
        self.assertEqual(works.call_count, 2)

    def test_software_encoding_can_be_forced(self):
        with (
            patch.dict("os.environ", {"CLIPFARMPILOT_VIDEO_ENCODER": "software"}),
            patch("backend.app.video._encoder_works") as works,
        ):
            self.assertIsNone(_hardware_encoder("ffmpeg"))
        works.assert_not_called()

    def test_a_failed_hardware_encode_is_redone_with_libx264(self):
        commands: list[list[str]] = []

        def fake_run(command):
            commands.append(command)
            if "h264_nvenc" in command:
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 0)

        with (
            patch("backend.app.video._hardware_encoder", return_value=("NVENC", video._nvenc_args)),
            patch("backend.app.video._run", side_effect=fake_run),
        ):
            used = _encode_video(lambda codec: ["ffmpeg", "-i", "in.mp4", *codec, "out.mp4"], 1920, 1080, Fraction(30), 18)

        self.assertEqual(used, "libx264")
        self.assertEqual([command[command.index("-c:v") + 1] for command in commands], ["h264_nvenc", "libx264"])

    def test_hardware_settings_track_the_requested_quality(self):
        self.assertIn("72", video._videotoolbox_quality_args(1920, 1080, Fraction(30), 18))
        self.assertIn("68", video._videotoolbox_quality_args(1920, 1080, Fraction(30), 20))
        high = video._videotoolbox_bitrate_args(1920, 1080, Fraction(30), 18)
        low = video._videotoolbox_bitrate_args(1920, 1080, Fraction(30), 20)
        self.assertGreater(int(high[high.index("-b:v") + 1]), int(low[low.index("-b:v") + 1]))


class FastExportTests(unittest.TestCase):
    @staticmethod
    def _make_video(path: Path, duration: float = 6.0) -> None:
        _run([
            ffmpeg_executable(), "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=s=320x180:r=30:d={duration}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duration}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
        ])

    def test_many_cuts_render_through_a_graph_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            output = root / "edited.mp4"
            self._make_video(source)
            # 40 cuts of 0.05 s every 0.15 s.
            cuts = [(0.10 + 0.15 * index, 0.15 + 0.15 * index) for index in range(40)]
            with (
                patch("backend.app.video._speech_pause_cut_intervals", return_value=cuts),
                patch("backend.app.video._INLINE_GRAPH_LIMIT", 200),
                patch("backend.app.video._run", wraps=_run) as run_command,
            ):
                export_clip(
                    source=source, output=output, start=0.0, end=6.0, aspect="16:9",
                    resolution="720p", edit_mode="full-length", remove_silence=True,
                    title_transcript=False, auto_sound_effect=False,
                )

            used_graph_file = any(
                "-/filter_complex" in call.args[0] or "-filter_complex_script" in call.args[0]
                for call in run_command.call_args_list
            )
            self.assertTrue(used_graph_file)
            self.assertAlmostEqual(probe_video(output).duration, 6.0 - 40 * 0.05, delta=0.12)

    def test_a_failing_audio_encode_reports_its_error_and_stops_the_decoder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            self._make_video(source, 2.0)
            command = [
                ffmpeg_executable(), "-v", "error", "-f", "s16le", "-ar", "48000", "-ac", "2", "-i", "pipe:0",
                "-c:a", "no-such-encoder", str(root / "audio.m4a"),
            ]

            with self.assertRaises(subprocess.CalledProcessError) as raised:
                _run_with_kept_audio(command, source, 0.0, 2.0, [(0.0, 1.5)])

            self.assertIn(b"no-such-encoder", raised.exception.stderr)

    def test_a_clip_with_only_sound_effects_copies_its_picture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            output = root / "clip.mp4"
            self._make_video(source, 4.0)
            with patch("backend.app.video._run", wraps=_run) as run_command:
                export_clip(
                    source=source, output=output, start=0.0, end=3.0, aspect="9:16",
                    sound_effect="vine-boom", auto_sound_effect=False, effect_time=1.0,
                    resolution="720p",
                )

            codecs = [
                call.args[0][call.args[0].index("-c:v") + 1]
                for call in run_command.call_args_list
                if "-c:v" in call.args[0]
            ]
            self.assertEqual(codecs.count("copy"), 1)
            self.assertEqual(len(codecs), 2)
            info = probe_video(output)
            self.assertEqual((info.width, info.height), (720, 1280))
            self.assertAlmostEqual(info.duration, 3.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
