from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

from backend.app.video import _run, _steady_audio_command, export_clip, ffmpeg_executable

RATE = 48_000


def _decode(command: list[str]) -> bytes:
    return subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout


def _channels(path: Path, channels: int = 2) -> np.ndarray:
    raw = _decode([ffmpeg_executable(), "-v", "error", "-i", str(path), "-map", "0:a:0", "-f", "s16le", "-ar",
                   str(RATE), "-"])
    return np.frombuffer(raw, dtype="<i2").astype(np.float64).reshape(-1, channels)


def _levels(samples: np.ndarray, start: float, end: float) -> list[float]:
    window = samples[int(start * RATE):int(end * RATE)]
    return [float(np.sqrt(np.mean(window[:, channel] ** 2))) for channel in range(window.shape[1])]


def _tone(samples: np.ndarray, channel: int) -> int:
    spectrum = np.abs(np.fft.rfft(samples[:, channel] * np.hanning(len(samples))))
    return round(int(np.argmax(spectrum)) * RATE / len(samples) / 10) * 10


class StereoSoundTests(unittest.TestCase):
    """Full-length edits are made in stereo; a mono recording keeps its level in both channels."""

    @classmethod
    def setUpClass(cls):
        cls._folder = tempfile.TemporaryDirectory()
        cls.root = Path(cls._folder.name)
        ffmpeg = ffmpeg_executable()
        picture = ["-f", "lavfi", "-i", "color=c=gray:s=320x180:r=30:d=4"]
        encode = ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest"]
        cls.mono = cls.root / "mono.mp4"
        _run([ffmpeg, "-y", "-v", "error", *picture,
              "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000:duration=4,volume=4", *encode, str(cls.mono)])
        cls.stereo = cls.root / "stereo.mp4"
        _run([ffmpeg, "-y", "-v", "error", *picture,
              "-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000:duration=4,volume=4",
              "-f", "lavfi", "-i", "sine=frequency=500:sample_rate=48000:duration=4,volume=2",
              "-filter_complex", "[1:a][2:a]join=inputs=2:channel_layout=stereo[a]", "-map", "0:v", "-map", "[a]",
              *encode, str(cls.stereo)])
        cls.surround = cls.root / "surround.mp4"
        tones = [f"sine=frequency={hz}:sample_rate=48000:duration=4" for hz in (200, 300, 400, 60, 500, 600)]
        _run([ffmpeg, "-y", "-v", "error", *picture, *[arg for tone in tones for arg in ("-f", "lavfi", "-i", tone)],
              "-filter_complex", "".join(f"[{i}:a]" for i in range(1, 7)) + "join=inputs=6:channel_layout=5.1(side)[a]",
              "-map", "0:v", "-map", "[a]", *encode, str(cls.surround)])

    @classmethod
    def tearDownClass(cls):
        cls._folder.cleanup()

    def steady(self, source: Path) -> bytes:
        return _decode(_steady_audio_command(ffmpeg_executable(), source, 0.0, 4.0))

    def ffmpeg_stereo(self, source: Path) -> bytes:
        return _decode([ffmpeg_executable(), "-v", "error", "-ss", "0.000", "-t", "4.000", "-i", str(source),
                        "-map", "0:a:0", "-vn", "-af", "aresample=async=1", "-ac", "2", "-ar", str(RATE),
                        "-f", "s16le", "-"])

    def full_length(self, source: Path) -> Path:
        output = self.root / f"full-{source.stem}.mp4"
        export_clip(source=source, output=output, start=0.0, end=4.0, aspect="16:9", resolution="720p",
                    edit_mode="full-length", title_transcript=False, auto_sound_effect=False)
        return output

    def test_mono_is_copied_into_both_channels_at_its_full_level(self):
        mono = np.frombuffer(_decode([ffmpeg_executable(), "-v", "error", "-t", "4.000", "-i", str(self.mono),
                                      "-map", "0:a:0", "-ar", str(RATE), "-f", "s16le", "-"]), dtype="<i2")
        stereo = np.frombuffer(self.steady(self.mono), dtype="<i2").reshape(-1, 2)

        self.assertEqual(len(stereo), len(mono))
        self.assertTrue(np.array_equal(stereo[:, 0], mono))
        self.assertTrue(np.array_equal(stereo[:, 1], mono))

    def test_stereo_and_surround_are_converted_exactly_as_ffmpeg_always_has(self):
        for source in (self.stereo, self.surround):
            with self.subTest(source=source.name):
                self.assertEqual(
                    hashlib.md5(self.steady(source)).hexdigest(), hashlib.md5(self.ffmpeg_stereo(source)).hexdigest()
                )

    def test_a_full_length_edit_of_a_mono_recording_is_as_loud_as_the_recording(self):
        source_level = _levels(_channels(self.mono, channels=1), 0.5, 3.5)[0]
        left, right = _levels(_channels(self.full_length(self.mono)), 0.5, 3.5)

        self.assertAlmostEqual(left / source_level, 1.0, delta=0.02)
        self.assertAlmostEqual(right / source_level, 1.0, delta=0.02)

    def test_a_full_length_edit_of_a_stereo_recording_keeps_each_channel(self):
        source = _channels(self.stereo)
        output = _channels(self.full_length(self.stereo))

        for channel, tone in ((0, 300), (1, 500)):
            self.assertAlmostEqual(
                _levels(output, 0.5, 3.5)[channel] / _levels(source, 0.5, 3.5)[channel], 1.0, delta=0.02
            )
            self.assertEqual(_tone(output[int(0.5 * RATE):int(3.5 * RATE)], channel), tone)

    def test_a_stream_that_switches_between_mono_and_stereo_keeps_each_part_at_its_level(self):
        # MPEG-TS carries its layout in-band, so joining the parts byte for byte
        # gives one file that runs mono, stereo, then mono again.
        pieces = []
        for index, (left, right) in enumerate(((330, None), (550, 880), (440, None))):
            part = self.root / f"part{index}.ts"
            audio = ["-f", "lavfi", "-i", f"sine=frequency={left}:sample_rate=48000:duration=2,volume=4"]
            if right is None:
                layout = ["-map", "0:v", "-map", "1:a"]
            else:
                audio += ["-f", "lavfi", "-i", f"sine=frequency={right}:sample_rate=48000:duration=2,volume=4"]
                layout = ["-filter_complex", "[1:a][2:a]join=inputs=2:channel_layout=stereo[a]",
                          "-map", "0:v", "-map", "[a]"]
            _run([ffmpeg_executable(), "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=320x180:r=30:d=2",
                  *audio, *layout, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                  "-output_ts_offset", str(2 * index), "-f", "mpegts", str(part)])
            pieces.append(part.read_bytes())
        source = self.root / "switching.ts"
        source.write_bytes(b"".join(pieces))
        output = self.root / "switching-full.mp4"
        export_clip(source=source, output=output, start=0.0, end=6.0, aspect="16:9", resolution="720p",
                    edit_mode="full-length", title_transcript=False, auto_sound_effect=False)

        samples = _channels(output)
        # Every part was made at the same level in each of its channels.
        levels = [level for start in (0.5, 2.5, 4.5) for level in _levels(samples, start, start + 1.0)]
        self.assertLess(max(levels) / min(levels), 1.03, levels)
        self.assertAlmostEqual(levels[0] / _levels(_channels(self.mono, channels=1), 0.5, 3.5)[0], 1.0, delta=0.03)
        part = samples[int(2.5 * RATE):int(3.5 * RATE)]
        self.assertEqual((_tone(part, 0), _tone(part, 1)), (550, 880))


if __name__ == "__main__":
    unittest.main()
