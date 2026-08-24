from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from backend.app.captions import (
    CaptionWord,
    group_caption_words,
    parse_whisper_words,
    write_live_caption_ass,
)
from backend.app.video import _apply_effects, _run, ffmpeg_executable


class LiveCaptionTests(unittest.TestCase):
    def test_whisper_output_becomes_bounded_word_timestamps(self):
        payload = {
            "transcription": [
                {"text": " Hello", "offsets": {"from": 100, "to": 420}},
                {"text": " world!", "offsets": {"from": 420, "to": 920}},
                {"text": "[_BEG_]", "offsets": {"from": 0, "to": 0}},
                {"text": " ignored", "offsets": {"from": 2_200, "to": 2_500}},
            ]
        }
        words = parse_whisper_words(payload, 2.0)
        self.assertEqual([word.text for word in words], ["Hello", "world!"])
        self.assertEqual((words[0].start, words[0].end), (0.1, 0.42))

    def test_caption_groups_break_on_sentences_gaps_and_readable_width(self):
        words = [
            CaptionWord("This", 0.0, 0.2),
            CaptionWord("is", 0.2, 0.35),
            CaptionWord("great.", 0.35, 0.7),
            CaptionWord("Another", 1.7, 2.0),
            CaptionWord("very", 2.0, 2.2),
            CaptionWord("clear", 2.2, 2.45),
            CaptionWord("caption", 2.45, 2.8),
        ]
        groups = group_caption_words(words)
        self.assertEqual([[word.text for word in group] for group in groups], [
            ["This", "is", "great."],
            ["Another", "very", "clear", "caption"],
        ])

    def test_ass_file_switches_the_highlight_colour_word_by_word(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            target = Path(temporary_name) / "captions.ass"
            write_live_caption_ass(
                [CaptionWord("Every", 0.1, 0.5), CaptionWord("word", 0.5, 0.9)],
                target,
                1080,
                1920,
                "pilot-lime",
            )
            content = target.read_text(encoding="utf-8")
        self.assertIn("PlayResX: 1080", content)
        self.assertIn("&H004AF3B9&", content)
        self.assertIn("Dialogue: 0,0:00:00.10,0:00:00.50", content)
        self.assertIn(r"{\c&H004AF3B9&}Every", content)
        self.assertIn(r"{\c&H00FFFFFF&}word", content)
        self.assertIn(r"{\c&H00FFFFFF&}Every", content)
        self.assertIn(r"{\c&H004AF3B9&}word", content)

    def test_live_caption_overlay_renders_and_changes_with_the_spoken_word(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            temporary = Path(temporary_name)
            source = temporary / "source.mp4"
            ass = temporary / "captions.ass"
            output = temporary / "captioned.mp4"
            first = temporary / "first.png"
            second = temporary / "second.png"
            ffmpeg = ffmpeg_executable(require_ass=True)
            _run([
                ffmpeg, "-y", "-v", "error", "-f", "lavfi",
                "-i", "color=c=0x202838:s=640x360:d=1.4:r=24",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", source,
            ])
            write_live_caption_ass(
                [CaptionWord("ONE", 0.1, 0.55), CaptionWord("TWO", 0.55, 1.05)],
                ass,
                640,
                360,
                "ocean",
            )
            _apply_effects(
                source, output, 1.3, 640, 360,
                "none", "none", 0.0, [], 1.0, 1.0, ass,
            )
            for timestamp, destination in ((0.25, first), (0.75, second)):
                _run([
                    ffmpeg, "-y", "-v", "error", "-ss", str(timestamp), "-i", str(output),
                    "-frames:v", "1", str(destination),
                ])
            first_pixels = np.asarray(Image.open(first).convert("RGB"), dtype=np.int16)
            second_pixels = np.asarray(Image.open(second).convert("RGB"), dtype=np.int16)
            difference = np.abs(first_pixels - second_pixels)
            self.assertGreater(int(np.count_nonzero(difference > 25)), 300)
            self.assertGreater(int(np.count_nonzero((first_pixels[:, :, 2] > 180) & (first_pixels[:, :, 0] < 120))), 30)


if __name__ == "__main__":
    unittest.main()
