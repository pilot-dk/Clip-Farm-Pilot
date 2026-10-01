from __future__ import annotations

import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from backend.app import video
from backend.app.captions import CaptionWord
from backend.app.profanity import is_profane, masked, profane_indexes
from backend.app.video import (
    _copy_kept_audio,
    _run,
    _swear_word_mutes,
    _without_swear_words,
    export_clip,
    ffmpeg_executable,
)

RATE = 48_000


class ProfanityWordTests(unittest.TestCase):
    def test_swear_words_are_recognised_in_the_forms_a_transcript_gives_them(self):
        for word in (
            "fuck", "Fucking,", "fuckin'", "motherfucker.", "bullshit", "Shit!", "bitches", "asshole",
            "dickhead", "pussy", "f***", "sh*t", "a**hole", "motherf***er", "****", "muthafucka", "Matherfica",
        ):
            self.assertTrue(is_profane(word), word)

    def test_ordinary_words_that_look_or_sound_alike_are_left_alone(self):
        for word in (
            "duck", "ship", "sheet", "beach", "pitch", "class", "assume", "bass", "cocktail", "peacock",
            "Dickens", "shiitake", "Scunthorpe", "mythical", "methodical", "ass", "badass",
            # Mild words do not affect monetisation.
            "damn", "hell", "crap",
            # Sound tags are not censored spellings.
            "*laughs*", "breathing*", "*heavy",
        ):
            self.assertFalse(is_profane(word), word)

    def test_a_word_split_in_two_is_found_as_a_pair(self):
        self.assertEqual(profane_indexes(["what", "an", "ass", "hole", "he", "is"]), [2, 3])
        self.assertEqual(profane_indexes(["kick", "ass", "play"]), [])

    def test_a_swear_word_is_shown_as_its_first_letter_and_asterisks(self):
        self.assertEqual(masked("Fucking,"), "F******,")
        self.assertEqual(masked("shit!"), "s***!")


class MutePlacementTests(unittest.TestCase):
    WORDS = [
        CaptionWord("what", 1.00, 1.20), CaptionWord("the", 1.20, 1.35), CaptionWord("fuck", 1.35, 1.70),
        CaptionWord("was", 1.70, 1.90), CaptionWord("that", 1.90, 2.20),
        CaptionWord("holy", 5.00, 5.30), CaptionWord("shit,", 5.30, 5.70), CaptionWord("bitch", 5.75, 6.10),
    ]

    def test_each_swear_word_is_muted_from_a_quarter_second_early(self):
        mutes, count = _swear_word_mutes(self.WORDS, 10.0)

        self.assertEqual(count, 3)
        # Whisper places these words late, so the mute starts 0.25 s before; the
        # two back-to-back words share one mute.
        self.assertEqual(mutes, [(1.10, 1.74), (5.05, 6.14)])

    def test_a_clean_transcript_mutes_nothing(self):
        self.assertEqual(_swear_word_mutes(self.WORDS[:2], 10.0), ([], 0))

    def test_captions_mask_swear_words_and_titles_leave_them_out(self):
        shown = [word.text for word in _without_swear_words(self.WORDS, keep_masked=True)]
        titled = [word.text for word in _without_swear_words(self.WORDS, keep_masked=False)]

        self.assertEqual(shown, ["what", "the", "f***", "was", "that", "holy", "s***,", "b****"])
        self.assertEqual(titled, ["what", "the", "was", "that", "holy"])

    def test_the_audio_stream_is_silent_inside_a_mute_and_untouched_outside(self):
        samples = np.full((3 * RATE, 2), 12_000, dtype="<i2")
        output = io.BytesIO()
        # One kept segment over a one-second read boundary, with a mute across it.
        _copy_kept_audio(io.BytesIO(samples.tobytes()), output, [(0.0, 3.0)], [(0.90, 1.30)])
        result = np.frombuffer(output.getvalue(), dtype="<i2").reshape(-1, 2)

        self.assertEqual(len(result), 3 * RATE)
        self.assertFalse(np.any(result[int(0.90 * RATE):int(1.30 * RATE)]))
        self.assertTrue(np.all(result[int(0.20 * RATE):int(0.85 * RATE)] == 12_000))
        self.assertTrue(np.all(result[int(1.35 * RATE):int(2.90 * RATE)] == 12_000))
        # A 12 ms ramp either side instead of a click.
        ramp = result[int(0.888 * RATE):int(0.90 * RATE), 0]
        self.assertTrue(np.all(np.diff(ramp.astype(np.int32)) <= 0))
        self.assertGreater(int(ramp[0]), int(ramp[-1]))


class MutedExportTests(unittest.TestCase):
    TRANSCRIPT = [
        CaptionWord("that", 0.40, 0.70), CaptionWord("was", 0.70, 0.95), CaptionWord("fucking", 2.00, 2.50),
        CaptionWord("close", 2.50, 2.90),
    ]

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.source = self.root / "source.mp4"
        _run([
            ffmpeg_executable(), "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=5",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=5",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(self.source),
        ])

    def _loudness(self, path: Path, start: float, end: float) -> float:
        raw = subprocess.run(
            [ffmpeg_executable(), "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "48000", "-f", "s16le", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float64)[int(start * RATE):int(end * RATE)]
        return float(np.sqrt(np.mean(samples ** 2)))

    def _assert_muted_only_at_the_swear_word(self, output: Path):
        # "fucking" at 2.00-2.50 s is muted from 1.75 s to 2.54 s.
        self.assertLess(self._loudness(output, 1.82, 2.48), 30)
        self.assertGreater(self._loudness(output, 0.30, 1.60), 1_000)
        self.assertGreater(self._loudness(output, 2.70, 4.50), 1_000)

    def test_a_clip_is_silent_only_where_the_swear_word_is(self):
        output = self.root / "clip.mp4"
        metadata: dict[str, object] = {}
        with patch.object(video, "transcribe_words", return_value=self.TRANSCRIPT):
            export_clip(
                source=self.source, output=output, start=0.0, end=5.0, aspect="9:16", resolution="720p",
                mute_profanity=True, title_transcript=True, export_metadata=metadata,
            )

        self._assert_muted_only_at_the_swear_word(output)
        self.assertEqual(metadata["muted_swear_words"], 1)
        self.assertEqual(metadata["title_transcript"], "that was close")

    def test_a_full_length_edit_is_silent_only_where_the_swear_word_is(self):
        output = self.root / "full.mp4"
        metadata: dict[str, object] = {}
        with patch.object(video, "transcribe_words", return_value=self.TRANSCRIPT), \
                patch.object(video, "write_live_caption_ass", wraps=video.write_live_caption_ass) as captions:
            export_clip(
                source=self.source, output=output, start=0.0, end=5.0, aspect="16:9", resolution="720p",
                edit_mode="full-length", mute_profanity=True, live_captions=True, title_transcript=True,
                auto_sound_effect=False, export_metadata=metadata,
            )

        self._assert_muted_only_at_the_swear_word(output)
        self.assertEqual(metadata["full_length_summary"]["muted_swear_words"], 1)
        self.assertEqual(metadata["title_transcript"], "that was close")
        self.assertEqual([word.text for word in captions.call_args.args[0]], ["that", "was", "f******", "close"])

    def test_the_switch_off_leaves_the_sound_alone(self):
        output = self.root / "clip.mp4"
        with patch.object(video, "transcribe_words", return_value=self.TRANSCRIPT):
            export_clip(
                source=self.source, output=output, start=0.0, end=5.0, aspect="9:16", resolution="720p",
                title_transcript=True,
            )
        self.assertGreater(self._loudness(output, 1.82, 2.48), 1_000)

    def test_muting_without_the_speech_engine_fails_instead_of_exporting_unmuted(self):
        for extra in ({"aspect": "9:16"}, {"aspect": "16:9", "edit_mode": "full-length"}):
            with patch.object(video, "transcribe_words", side_effect=RuntimeError("engine missing")):
                with self.assertRaisesRegex(ValueError, "Muting swear words needs"):
                    export_clip(
                        source=self.source, output=self.root / "out.mp4", start=0.0, end=5.0,
                        resolution="720p", mute_profanity=True, **extra,
                    )


if __name__ == "__main__":
    unittest.main()
