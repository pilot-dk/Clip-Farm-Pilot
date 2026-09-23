from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from backend.app import captions, video
from backend.app.captions import (
    DTW_ONSET_LEAD_SECONDS,
    FILLER_PROMPT,
    CaptionWord,
    _merge_segments,
    parse_whisper_words,
)
from backend.app.video import (
    _filler_extent,
    _filler_word_cut_intervals,
    _speech_frame_seconds,
    _spare_spoken_words,
    _speech_pause_cut_intervals,
    _tighten_speech_edges,
)

FRAME = _speech_frame_seconds()
CENTRE = video._SPEECH_WINDOW_SAMPLES / (2 * video._SPEECH_SAMPLE_RATE)


def level_track(duration: float, spans: list[tuple[float, float, float]], floor: float = -20.0) -> np.ndarray:
    """10 ms voice-band levels: `floor` everywhere, `level` inside each (start, end, level)."""
    count = int(round(duration / FRAME))
    levels = np.full(count, floor, dtype=np.float32)
    times = np.arange(count) * FRAME + CENTRE
    for start, end, level in spans:
        levels[(times >= start) & (times < end)] = level
    return levels


def covered(cuts: list[tuple[float, float]], start: float, end: float) -> float:
    return sum(max(0.0, min(end, b) - max(start, a)) for a, b in cuts)


class SpeechPauseTests(unittest.TestCase):
    def _cuts(self, segments, levels, duration):
        with patch.object(video, "_speech_segments", return_value=segments), \
                patch.object(video, "_speech_band_levels", return_value=levels), \
                patch.object(video, "_analysis_cache_key", side_effect=lambda *a, **k: object()):
            return _speech_pause_cut_intervals(Path("unused.mp4"), 0.0, duration)

    def test_a_pause_between_sentences_is_tightened_even_over_game_audio(self):
        # Speech 0-2 s and 3.2-5 s; the game audio in between is only 12 dB quieter.
        levels = level_track(5.0, [(0.0, 2.0, 20.0), (3.2, 5.0, 20.0)], floor=8.0)
        cuts = self._cuts([(0.0, 2.0), (3.2, 5.0)], levels, 5.0)
        self.assertEqual(len(cuts), 1)
        start, end = cuts[0]
        # A breath is kept on both sides: more after speech ends than before it resumes.
        self.assertAlmostEqual(start, 2.0 + 0.15, places=2)
        self.assertAlmostEqual(end, 3.2 - 0.10, places=2)

    def test_a_long_stretch_of_gameplay_without_talking_is_kept(self):
        levels = level_track(12.0, [(0.0, 1.5, 20.0), (10.0, 12.0, 20.0)], floor=6.0)
        cuts = self._cuts([(0.0, 1.5), (10.0, 12.0)], levels, 12.0)
        self.assertEqual(covered(cuts, 1.5, 10.0), 0.0)

    def test_dead_air_inside_a_long_gap_is_removed(self):
        # Five seconds of near silence (40 dB under the speech) between two sentences.
        levels = level_track(8.0, [(0.0, 1.5, 20.0), (6.5, 8.0, 20.0)], floor=-20.0)
        cuts = self._cuts([(0.0, 1.5), (6.5, 8.0)], levels, 8.0)
        self.assertGreater(covered(cuts, 1.5, 6.5), 4.5)
        self.assertEqual(covered(cuts, 0.0, 1.5) + covered(cuts, 6.5, 8.0), 0.0)

    def test_an_intro_before_the_first_word_keeps_its_audio(self):
        # Two seconds of music, then speech: not a pause between sentences.
        levels = level_track(4.0, [(0.0, 2.0, 12.0), (2.0, 4.0, 20.0)], floor=-20.0)
        cuts = self._cuts([(2.0, 4.0)], levels, 4.0)
        self.assertEqual(covered(cuts, 0.0, 2.0), 0.0)

    def test_silence_is_still_removed_when_nobody_speaks(self):
        # Music, one second of silence, music: judged against the loudest audio.
        levels = level_track(4.0, [(0.0, 1.0, 18.0), (2.0, 4.0, 18.0)], floor=-40.0)
        cuts = self._cuts([], levels, 4.0)
        self.assertGreater(covered(cuts, 1.0, 2.0), 0.7)
        self.assertEqual(covered(cuts, 0.0, 1.0) + covered(cuts, 2.0, 4.0), 0.0)


class MissedWordTests(unittest.TestCase):
    # Speech detected at 0-2 s and 3.2-5 s, with game audio 12 dB under the voice between.
    SEGMENTS = [(0.0, 2.0), (3.2, 5.0)]
    CUTS = [(2.15, 3.10)]

    def _spare(self, words, levels=None):
        if levels is None:
            levels = level_track(5.0, [(0.0, 2.0, 20.0), (2.4, 2.8, 20.0), (3.2, 5.0, 20.0)], floor=8.0)
        return _spare_spoken_words(self.CUTS, words, levels, self.SEGMENTS, 5.0)

    def test_a_word_the_detector_missed_keeps_its_breath_and_the_rest_of_the_pause_goes(self):
        cuts = self._spare([CaptionWord("Let's", 2.45, 2.75)])
        self.assertEqual(covered(cuts, 2.45, 2.75), 0.0)
        # The same breath as detected speech: 0.10 s before the word and 0.15 s after it.
        self.assertEqual(cuts, [(2.15, 2.35), (2.9, 3.1)])

    def test_a_word_the_detector_heard_is_left_to_its_edges(self):
        # The last word's estimated end runs into the pause, but it starts inside
        # detected speech, so the cut stays where the detector put it.
        self.assertEqual(self._spare([CaptionWord("left", 1.70, 2.40)]), self.CUTS)

    def test_a_first_word_heard_just_before_the_speech_keeps_its_own_breath(self):
        # "You" starts 0.12 s before the detected sentence, earlier than its breath reaches.
        cuts = self._spare([CaptionWord("You", 3.08, 3.14), CaptionWord("know", 3.14, 3.40)])
        self.assertEqual(cuts, [(2.15, 2.98)])

    def test_silent_words_sound_tags_and_fillers_are_still_cut(self):
        quiet_gap = level_track(5.0, [(0.0, 2.0, 20.0), (3.2, 5.0, 20.0)], floor=-30.0)
        self.assertEqual(self._spare([CaptionWord("Okay.", 2.45, 2.75)], quiet_gap), self.CUTS)
        tag = [CaptionWord("[sounds", 2.40, 2.50), CaptionWord("of", 2.50, 2.60), CaptionWord("running]", 2.60, 2.80)]
        self.assertEqual(self._spare(tag), self.CUTS)
        self.assertEqual(self._spare([CaptionWord("um,", 2.45, 2.75)]), self.CUTS)

    def test_a_tag_left_open_stops_hiding_words_after_eight(self):
        words = [CaptionWord("[music", 0.1, 0.2)] + [CaptionWord("la", 0.2 + 0.1 * n, 0.3 + 0.1 * n) for n in range(7)]
        words.append(CaptionWord("Let's", 2.45, 2.75))
        self.assertEqual(covered(self._spare(words), 2.45, 2.75), 0.0)

    def test_a_full_length_export_transcribes_for_pause_removal_and_spares_missed_words(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, output = Path(temporary) / "source.mp4", Path(temporary) / "edited.mp4"
            video._run([
                video.ffmpeg_executable(), "-y", "-v", "error",
                "-f", "lavfi", "-i", "color=c=navy:s=320x180:r=30:d=5",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=5",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source),
            ])
            levels = level_track(5.0, [], floor=20.0)
            with patch.object(video, "_speech_pause_cut_intervals", return_value=[(1.0, 3.0)]), \
                    patch.object(video, "_speech_context", return_value=(levels, [(0.0, 1.0), (3.0, 5.0)])), \
                    patch.object(video, "transcribe_words", return_value=[CaptionWord("hey", 1.9, 2.1)]) as transcribe:
                video.export_clip(
                    source=source, output=output, start=0.0, end=5.0, aspect="16:9", resolution="720p",
                    edit_mode="full-length", remove_silence=True, title_transcript=False, auto_sound_effect=False,
                )
            transcribe.assert_called_once()
            # 1.0-1.8 and 2.25-3.0 are cut; "hey" and its breath stay.
            self.assertAlmostEqual(video.probe_video(output).duration, 5.0 - 0.8 - 0.75, delta=0.1)


class SpeechEdgeTests(unittest.TestCase):
    def test_padding_is_trimmed_fully_at_the_start_and_gently_at_the_end(self):
        # Voice from 1.0 to 2.0 s with a soft tail to 2.2 s; the detector padded 0.3 s each side.
        levels = level_track(4.0, [(1.0, 2.0, 20.0), (2.0, 2.2, 3.0)], floor=0.0)
        tightened = _tighten_speech_edges([(0.7, 2.5)], levels, 4.0)
        start, end = tightened[0]
        self.assertAlmostEqual(start, 1.0, delta=0.05)
        # The soft tail (3 dB above the background) is kept.
        self.assertGreater(end, 2.15)
        self.assertLessEqual(end, 2.5)

    def test_edges_only_move_inward(self):
        levels = level_track(3.0, [(0.5, 2.5, 20.0)], floor=0.0)
        start, end = _tighten_speech_edges([(1.0, 2.0)], levels, 3.0)[0]
        self.assertGreaterEqual(start, 1.0)
        self.assertLessEqual(end, 2.0)


class FillerPlacementTests(unittest.TestCase):
    def test_a_drawn_out_uhhh_with_a_dip_inside_is_cut_completely(self):
        # "know" to 1.0 s, a 1.0 s "uhhh" with a shallow dip halfway, then "what" at 2.1 s.
        levels = level_track(3.0, [
            (0.2, 1.0, 22.0), (1.05, 1.45, 24.0), (1.45, 1.55, 10.0), (1.55, 2.05, 22.0), (2.1, 3.0, 22.0),
        ], floor=-20.0)
        words = [CaptionWord("know", 0.2, 1.0), CaptionWord("uhh", 1.05, 1.44), CaptionWord("what", 2.1, 2.5)]
        cuts, count = _filler_word_cut_intervals(words, 3.0, levels, [(0.2, 3.0)])
        self.assertEqual(count, 1)
        self.assertGreater(covered(cuts, 1.05, 2.05), 0.95)
        self.assertLess(covered(cuts, 2.1, 3.0) + covered(cuts, 0.2, 1.0), 0.03)

    def test_a_next_word_placed_late_is_not_clipped(self):
        # "um" 1.0-1.3 s, gap, "alright" really starts at 1.4 s but is transcribed at 1.55 s.
        levels = level_track(3.0, [(0.2, 0.9, 22.0), (1.0, 1.3, 22.0), (1.4, 3.0, 22.0)], floor=-20.0)
        words = [CaptionWord("insane", 0.2, 0.9), CaptionWord("um", 1.0, 1.3), CaptionWord("alright", 1.55, 2.0)]
        cuts, _ = _filler_word_cut_intervals(words, 3.0, levels, [(0.2, 3.0)])
        self.assertGreater(covered(cuts, 1.0, 1.3), 0.27)
        self.assertLess(covered(cuts, 1.4, 3.0), 0.02)

    def test_filler_extent_reports_nothing_without_a_clear_gap(self):
        levels = level_track(2.0, [(0.0, 2.0, 20.0)], floor=20.0)
        self.assertEqual(_filler_extent(levels, 0.8, 1.4), (None, None))

    def test_without_audio_the_transcript_placement_is_used(self):
        words = [CaptionWord("so", 0.0, 0.3), CaptionWord("um", 0.4, 0.7), CaptionWord("yes", 0.9, 1.2)]
        with_audio_off, count = _filler_word_cut_intervals(words, 2.0)
        self.assertEqual(count, 1)
        self.assertGreater(covered(with_audio_off, 0.4, 0.7), 0.29)


class TranscriptTimingTests(unittest.TestCase):
    @staticmethod
    def _segment(text: str, offset_from: int, offset_to: int, t_dtw: int) -> dict:
        return {
            "text": text,
            "offsets": {"from": offset_from, "to": offset_to},
            "tokens": [
                {"text": "[_BEG_]", "t_dtw": -1},
                {"text": text, "t_dtw": t_dtw},
            ],
        }

    def test_words_are_placed_by_their_dtw_alignment(self):
        payload = {"transcription": [
            # Whisper's own offsets are far off; DTW marks each word 0.13 s after it starts.
            self._segment(" So", 0, 400, 43),
            self._segment(" um,", 900, 1000, 296),
            self._segment(" and", 1000, 1400, 318),
        ]}
        words = parse_whisper_words(payload, 5.0)
        self.assertEqual([w.text for w in words], ["So", "um,", "and"])
        self.assertAlmostEqual(words[1].start, 2.96 - DTW_ONSET_LEAD_SECONDS, places=3)
        # A word lasts until the next begins...
        self.assertAlmostEqual(words[1].end, words[2].start, places=3)
        # ...or only its likely length when a pause follows it.
        self.assertAlmostEqual(words[0].end, words[0].start + 0.30, places=3)

    def test_without_dtw_data_whisper_offsets_are_used(self):
        payload = {"transcription": [{"text": " Hello", "offsets": {"from": 100, "to": 420}}]}
        words = parse_whisper_words(payload, 2.0)
        self.assertEqual((words[0].start, words[0].end), (0.1, 0.42))

    def _commands(self, include_fillers: bool = False, gpu_fails: bool = False) -> list[list[str]]:
        seen: list[list[str]] = []

        def fake_run(command, **kwargs):
            seen.append([str(part) for part in command])
            if "-of" in command:
                if gpu_fails and "-ng" not in command:
                    return subprocess.CompletedProcess(command, 1, b"", b"ggml_metal_init: error")
                Path(str(command[command.index("-of") + 1]) + ".json").write_text('{"transcription": []}')
            else:
                Path(str(command[-1])).write_bytes(b"\0" * 256)
            return subprocess.CompletedProcess(command, 0, b"", b"")

        with tempfile.TemporaryDirectory() as temporary:
            cli, model = Path(temporary) / "whisper-cli", Path(temporary) / "model.bin"
            cli.write_text("")
            model.write_text("")
            with patch.object(captions, "caption_runtime_paths", return_value=(cli, model)), \
                    patch.object(captions.subprocess, "run", side_effect=fake_run):
                captions.transcribe_words(Path("clip.mp4"), 0.0, 5.0, "ffmpeg", include_fillers=include_fillers)
        return [command for command in seen if "-of" in command]

    def _command(self, include_fillers: bool) -> list[str]:
        return self._commands(include_fillers)[0]

    def test_a_mac_transcribes_on_the_gpu_and_falls_back_to_the_cpu(self):
        with patch.object(captions.sys, "platform", "darwin"):
            self.assertEqual(len(self._commands()), 1)
            self.assertNotIn("-ng", self._commands()[0])
            retried = self._commands(gpu_fails=True)
        self.assertEqual(len(retried), 2)
        self.assertIn("-ng", retried[1])
        with patch.object(captions.sys, "platform", "win32"):
            self.assertIn("-ng", self._commands()[0])

    def test_transcription_uses_dtw_timing(self):
        command = self._command(include_fillers=False)
        for flag in ("-nfa", "-ojf", "-dtw"):
            self.assertIn(flag, command)
        self.assertEqual(command[command.index("-dtw") + 1], "base.en")

    def test_the_filler_prompt_is_only_used_for_filler_removal(self):
        self.assertNotIn("--prompt", self._command(include_fillers=False))
        with_fillers = self._command(include_fillers=True)
        self.assertEqual(with_fillers[with_fillers.index("--prompt") + 1], FILLER_PROMPT)
        self.assertIn(" um,", FILLER_PROMPT)

    def test_the_cached_transcript_is_kept_separately_for_filler_removal(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "clip.mp4"
            source.write_bytes(b"x")
            plain = [CaptionWord("hello", 0.0, 0.3)]
            disfluent = [CaptionWord("um", 0.0, 0.3)]
            with patch("backend.app.video.transcribe_words", side_effect=[plain, disfluent]) as transcribe:
                self.assertEqual(video._transcribe_words_cached(source, 0.0, 1.0), plain)
                self.assertEqual(video._transcribe_words_cached(source, 0.0, 1.0, include_fillers=True), disfluent)
                self.assertEqual(video._transcribe_words_cached(source, 0.0, 1.0), plain)
            self.assertEqual(transcribe.call_count, 2)
            self.assertTrue(transcribe.call_args_list[1].kwargs.get("include_fillers"))


class SpeechDetectorTests(unittest.TestCase):
    OUTPUT = (
        "read_audio_data: reading audio data\n"
        "Detected 2 speech segments:\n"
        "Speech segment 0: start = 29.00, end = 496.00\n"
        "Speech segment 1: start = 589.00, end = 1021.00\n"
    )

    def test_detector_output_is_read_in_seconds(self):
        completed = subprocess.CompletedProcess([], 0, self.OUTPUT.encode(), b"")
        with patch.object(captions.subprocess, "run", return_value=completed):
            segments = captions._run_speech_detector(Path("tool"), Path("model"), Path("audio.wav"))
        self.assertEqual(segments, [(0.29, 4.96), (5.89, 10.21)])

    def test_a_failed_run_is_reported(self):
        completed = subprocess.CompletedProcess([], 1, b"error: cannot load model\n", b"")
        with patch.object(captions.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "cannot load model"):
                captions._run_speech_detector(Path("tool"), Path("model"), Path("audio.wav"))

    def test_overlapping_pieces_of_a_long_recording_merge(self):
        # Speech crossing a ten-minute boundary shows up in both overlapping pieces.
        merged = _merge_segments([(598.0, 603.5), (0.5, 4.0), (600.0, 606.0), (5.0, 7.0)], 700.0)
        self.assertEqual(merged, [(0.5, 4.0), (5.0, 7.0), (598.0, 606.0)])

    def test_long_recordings_are_analysed_in_overlapping_pieces(self):
        pieces: list[tuple[float, float]] = []

        def fake_run(command, **kwargs):
            if "-ss" in command:
                pieces.append((float(command[command.index("-ss") + 1]), float(command[command.index("-t") + 1])))
                Path(command[-1]).write_bytes(b"\0" * 64)
                return subprocess.CompletedProcess(command, 0, b"", b"")
            return subprocess.CompletedProcess(command, 0, b"Detected 0 speech segments:\n", b"")

        with tempfile.TemporaryDirectory() as temporary:
            tool, model = Path(temporary) / "vad", Path(temporary) / "silero.bin"
            tool.write_text("")
            model.write_text("")
            with patch.object(captions, "speech_detector_paths", return_value=(tool, model)), \
                    patch.object(captions.subprocess, "run", side_effect=fake_run):
                captions.detect_speech_segments(Path("long.mp4"), 10.0, 10.0 + 1500.0, "ffmpeg")
        self.assertEqual([round(start) for start, _ in pieces], [10, 610, 1210])
        self.assertTrue(all(length <= 604.0 for _, length in pieces))

    def test_a_missing_detector_falls_back_to_the_loudness_detector(self):
        levels = level_track(3.0, [(0.3, 1.0, 20.0), (2.0, 2.8, 20.0)], floor=-20.0)
        with patch.object(video, "detect_speech_segments", side_effect=ValueError("missing")), \
                patch.object(video, "_speech_band_levels", return_value=levels), \
                patch.object(video, "_analysis_cache_key", side_effect=lambda *a, **k: object()):
            segments = video._speech_segments(Path("unused.mp4"), 0.0, 3.0)
        self.assertEqual(len(segments), 2)
        self.assertAlmostEqual(segments[0][0], 0.3, delta=0.05)
        self.assertAlmostEqual(segments[1][1], 2.8, delta=0.05)


class RuntimePackagingTests(unittest.TestCase):
    def test_every_platform_ships_the_speech_detector(self):
        root = Path(__file__).resolve().parents[1]
        preparer = (root / "scripts" / "prepare_caption_runtime.py").read_text(encoding="utf-8")
        self.assertIn('TOOLS = ("whisper-cli", "whisper-vad-speech-segments")', preparer)
        self.assertIn('VAD_MODEL_NAME = "ggml-silero-v6.2.0.bin"', preparer)
        self.assertIn('VAD_MODEL_SHA256 = "2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987"', preparer)
        self.assertIn('raise RuntimeError(f"The prepared caption runtime is missing {executable.name}.")', preparer)
        self.assertEqual(preparer.count('"--target", *TOOLS'), 3)

    def test_the_packaged_self_test_runs_the_speech_detector(self):
        with patch.object(captions, "speech_detector_paths", return_value=(Path("/missing/tool"), Path("/missing/model"))):
            with self.assertRaisesRegex(RuntimeError, "speech detector is missing"):
                captions.speech_detector_self_test()


if __name__ == "__main__":
    unittest.main()
