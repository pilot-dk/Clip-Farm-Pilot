from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from backend.app import main, video
from backend.app.captions import CaptionWord
from backend.app.video import (
    _probed_frame_rate,
    _run,
    export_clip,
    ffmpeg_executable,
    format_frame_rate,
    normalize_frame_rate,
    output_size,
    probe_video,
)


def _png_data_url(size: int) -> str:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    image.paste((255, 255, 255, 255), (size // 4, size // 3, size * 3 // 4, size // 2))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


class OutputSizeTests(unittest.TestCase):
    def test_every_shape_and_resolution_keeps_its_proportions(self):
        expected = {
            ("16:9", "720p"): (1280, 720),
            ("16:9", "1080p"): (1920, 1080),
            ("16:9", "2160p"): (3840, 2160),
            ("9:16", "720p"): (720, 1280),
            ("9:16", "1080p"): (1080, 1920),
            ("9:16", "2160p"): (2160, 3840),
            ("1:1", "720p"): (720, 720),
            ("1:1", "1080p"): (1080, 1080),
            ("1:1", "2160p"): (2160, 2160),
        }
        for (aspect, resolution), size in expected.items():
            with self.subTest(aspect=aspect, resolution=resolution):
                self.assertEqual(output_size(aspect, resolution), size)
                # H.264 in yuv420p needs even dimensions.
                self.assertTrue(all(side % 2 == 0 for side in size))

    def test_1080p_is_the_default_so_existing_exports_are_unchanged(self):
        self.assertEqual(output_size("16:9"), (1920, 1080))
        self.assertEqual(main.ExportRequest(start=0, end=12).resolution, "1080p")

    def test_unknown_shapes_and_resolutions_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "resolution"):
            output_size("16:9", "8k")
        with self.assertRaisesRegex(ValueError, "aspect"):
            output_size("4:3", "1080p")


class FrameRateDetectionTests(unittest.TestCase):
    def test_probed_rates_become_exact_rates_including_ntsc(self):
        cases = {
            "24": Fraction(24),
            24.0: Fraction(24),
            "25/1": Fraction(25),
            30: Fraction(30),
            "30000/1001": Fraction(30000, 1001),
            29.97: Fraction(30000, 1001),
            23.976: Fraction(24000, 1001),
            59.94: Fraction(60000, 1001),
            "60": Fraction(60),
            "120/1": Fraction(120),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_frame_rate(value), expected)

    def test_unusable_rates_are_ignored_instead_of_guessed(self):
        for value in (None, "", "0/0", 0, -30, "abc", 1_000):
            with self.subTest(value=value):
                self.assertIsNone(normalize_frame_rate(value))

    def test_rates_format_the_way_ffmpeg_reads_them(self):
        self.assertEqual(format_frame_rate(Fraction(60)), "60")
        self.assertEqual(format_frame_rate(Fraction(30000, 1001)), "30000/1001")
        self.assertEqual(format_frame_rate(None), "")

    def test_variable_rate_recordings_use_their_average_rate(self):
        # Constant-rate files agree, so the exact nominal rate wins.
        self.assertEqual(_probed_frame_rate("30000/1001", "29993/1001"), Fraction(30000, 1001))
        # Phone and capture files often report a 90 fps timebase over a ~30 fps average.
        self.assertEqual(_probed_frame_rate("90/1", "2997/100"), Fraction(30000, 1001))
        self.assertEqual(_probed_frame_rate("0/0", "25/1"), Fraction(25))


class ResolutionRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls._temporary.name)
        cls.ffprobe = shutil.which("ffprobe")
        cls.sources = {rate: cls._make_source(rate) for rate in ("60", "30000/1001")}

    @classmethod
    def tearDownClass(cls):
        cls._temporary.cleanup()

    @classmethod
    def _make_source(cls, rate: str) -> Path:
        path = cls.root / f"source-{rate.replace('/', '_')}.mp4"
        _run([
            ffmpeg_executable(), "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate={rate}:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
        ])
        return path

    def _stream(self, path: Path) -> dict:
        if not self.ffprobe:
            self.skipTest("ffprobe is needed to count rendered frames.")
        result = subprocess.run(
            [
                self.ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
                "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate,nb_read_frames,pix_fmt",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, check=True,
        )
        return json.loads(result.stdout)["streams"][0]

    def _assert_render(self, path: Path, size: tuple[int, int], rate: str, seconds: float) -> None:
        stream = self._stream(path)
        self.assertEqual((int(stream["width"]), int(stream["height"])), size)
        self.assertEqual(Fraction(stream["r_frame_rate"]), Fraction(rate))
        self.assertEqual(Fraction(stream["avg_frame_rate"]), Fraction(rate))
        self.assertLessEqual(abs(int(stream["nb_read_frames"]) - float(Fraction(rate)) * seconds), 1)
        self.assertEqual(stream["pix_fmt"], "yuv420p")

    def test_clip_layouts_render_every_resolution_at_the_source_frame_rate(self):
        cases = (
            ("standard-720", dict(aspect="16:9", resolution="720p"), (1280, 720)),
            ("vertical-4k", dict(aspect="9:16", resolution="2160p"), (2160, 3840)),
            ("square-caption-4k", dict(aspect="1:1", resolution="2160p", caption_text="4K"), (2160, 2160)),
            ("effects-720", dict(aspect="16:9", resolution="720p", visual_effect="lens-flare",
                                 sound_effect="vine-boom", auto_sound_effect=False, effect_time=0.4), (1280, 720)),
            ("zoom-4k", dict(aspect="16:9", resolution="2160p", visual_effect="punch-zoom", effect_time=0.3), (3840, 2160)),
        )
        for rate, source in self.sources.items():
            for name, options, size in cases:
                with self.subTest(rate=rate, case=name):
                    output = self.root / f"{name}-{rate.replace('/', '_')}.mp4"
                    export_clip(source=source, output=output, start=0.0, end=1.0, **options)
                    self._assert_render(output, size, rate, 1.0)

    def test_gaming_layout_keeps_the_source_frame_rate(self):
        # The layout used to composite onto a generated background that forced 25 fps.
        for rate, source in self.sources.items():
            for resolution, size in (("720p", (720, 1280)), ("2160p", (2160, 3840))):
                with self.subTest(rate=rate, resolution=resolution):
                    output = self.root / f"gaming-{resolution}-{rate.replace('/', '_')}.mp4"
                    export_clip(
                        source=source, output=output, start=0.0, end=1.0,
                        aspect="9:16", layout="gaming", resolution=resolution,
                    )
                    self._assert_render(output, size, rate, 1.0)

    def test_full_length_edits_render_every_resolution_at_the_source_frame_rate(self):
        for rate, source in self.sources.items():
            for aspect, resolution, size in (("16:9", "720p", (1280, 720)), ("1:1", "2160p", (2160, 2160))):
                with self.subTest(rate=rate, aspect=aspect, resolution=resolution):
                    output = self.root / f"full-{aspect.replace(':', 'x')}-{resolution}-{rate.replace('/', '_')}.mp4"
                    metadata: dict[str, object] = {}
                    export_clip(
                        source=source, output=output, start=0.0, end=1.5,
                        aspect=aspect, resolution=resolution, edit_mode="full-length",
                        subscribe_animation=True, caption_text="FULL" if aspect == "1:1" else "",
                        export_metadata=metadata,
                    )
                    self._assert_render(output, size, rate, 1.5)
                    self.assertEqual((metadata["width"], metadata["height"]), size)
                    self.assertEqual(metadata["frame_rate"], rate)
                    self.assertEqual(metadata["resolution"], resolution)

    def test_frame_rate_is_read_without_ffprobe_like_the_packaged_apps(self):
        source = self.sources["30000/1001"]
        with patch.object(video.shutil, "which", return_value=None), patch.dict(
            "os.environ", {"CLIPFARMPILOT_FFPROBE_EXE": ""}
        ):
            self.assertEqual(probe_video(source).frame_rate, Fraction(30000, 1001))
            output = self.root / "fallback-probe.mp4"
            metadata: dict[str, object] = {}
            export_clip(source=source, output=output, start=0.0, end=1.0, aspect="16:9",
                        resolution="720p", export_metadata=metadata)
        self.assertEqual(metadata["frame_rate"], "30000/1001")
        self._assert_render(output, (1280, 720), "30000/1001", 1.0)

    def test_live_captions_keep_their_place_and_size_at_every_resolution(self):
        source = self.root / "black.mp4"
        _run([
            ffmpeg_executable(), "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=black:s=640x360:r=30:d=1.2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
        ])
        words = [CaptionWord("CAPTION", 0.0, 1.0)]
        extents = {}
        for resolution in ("720p", "2160p"):
            output = self.root / f"live-{resolution}.mp4"
            frame = self.root / f"live-{resolution}.png"
            with patch("backend.app.video.transcribe_words", return_value=words):
                export_clip(
                    source=source, output=output, start=0.0, end=1.0, aspect="16:9",
                    resolution=resolution, live_captions=True,
                    live_caption_height=0.3, live_caption_scale=1.25,
                )
            _run([ffmpeg_executable(), "-y", "-v", "error", "-ss", "0.5", "-i", str(output),
                  "-frames:v", "1", str(frame)])
            pixels = np.asarray(Image.open(frame).convert("L"))
            rows, columns = np.nonzero(pixels > 120)
            self.assertGreater(rows.size, 0, f"The {resolution} caption was not drawn.")
            height, width = pixels.shape
            extents[resolution] = (
                rows.min() / height, rows.max() / height, columns.min() / width, columns.max() / width,
            )
        for small, large in zip(extents["720p"], extents["2160p"]):
            self.assertAlmostEqual(small, large, delta=0.01)


class ResolutionApiTests(unittest.TestCase):
    def test_resolution_is_validated(self):
        for value in ("720p", "1080p", "2160p"):
            with self.subTest(value=value):
                self.assertEqual(main.ExportRequest(start=0, end=12, resolution=value).resolution, value)
        for value in ("480p", "4k", "1440p"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                main.ExportRequest(start=0, end=12, resolution=value)

    def test_square_caption_images_are_accepted_at_every_export_size(self):
        for size in (720, 1080, 2160):
            with self.subTest(size=size):
                overlay = main._caption_overlay_from_data_url(_png_data_url(size), "bottom")
                try:
                    with Image.open(overlay) as image:
                        self.assertEqual(image.size, (size, size))
                finally:
                    overlay.unlink(missing_ok=True)
        with self.assertRaisesRegex(ValueError, "720, 1080, or 2160"):
            main._caption_overlay_from_data_url(_png_data_url(1440))


class ResolutionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (Path(__file__).resolve().parents[1] / "backend" / "app" / "static" / "index.html").read_text(
            encoding="utf-8"
        )

    def test_resolution_picker_offers_720p_1080p_and_4k_in_both_workspaces(self):
        self.assertIn('name="resolution" value="720p"', self.html)
        self.assertIn('name="resolution" value="1080p" checked', self.html)
        self.assertIn('name="resolution" value="2160p" /><span>4K</span>', self.html)
        self.assertIn("resolution: state.resolution", self.html)
        # The picker sits outside the ratio grid, which is the part the full-length workspace trims.
        picker = self.html.index('id="resolutionControl"')
        self.assertGreater(picker, self.html.index('id="fullLengthFormat"'))

    def test_the_source_frame_rate_is_shown_and_square_captions_are_drawn_at_full_size(self):
        self.assertIn("Frame rate stays at the source's", self.html)
        self.assertIn("state.sourceFrameRate = data.frame_rate", self.html)
        self.assertIn('const [squareSize] = exportSize("1:1");', self.html)
        self.assertIn("context.setTransform(1, 0, 0, 1, 0, 0);", self.html)


if __name__ == "__main__":
    unittest.main()
