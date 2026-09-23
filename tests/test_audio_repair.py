from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from backend.app import video
from backend.app.video import _run, audio_source, export_clip, ffmpeg_executable, probe_video


def _mark_mono(adts: bytes) -> bytes:
    """Rewrite every ADTS header to declare one channel, whatever its frame holds."""
    frames = bytearray()
    position = 0
    while position + 7 <= len(adts):
        length = ((adts[position + 3] & 0x03) << 11) | (adts[position + 4] << 3) | (adts[position + 5] >> 5)
        frame = bytearray(adts[position:position + length])
        frame[2] &= 0xFE
        frame[3] = (frame[3] & 0x3F) | (1 << 6)
        frames += frame
        position += length
    return bytes(frames)


def _pcm(path: Path, start: float = 0.0) -> np.ndarray:
    raw = subprocess.run(
        [ffmpeg_executable(), "-v", "error", "-ss", f"{start:.3f}", "-i", str(path), "-vn",
         "-ac", "2", "-ar", "48000", "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    ).stdout
    return np.frombuffer(raw, dtype="<i2").reshape(-1, 2).astype(np.float64)


def _tone(samples: np.ndarray, second: float) -> tuple[int, int]:
    """The strongest frequency in each channel over half a second around `second`."""
    window = samples[int((second - 0.25) * 48_000):int((second + 0.25) * 48_000)]
    peaks = []
    for channel in range(2):
        spectrum = np.abs(np.fft.rfft(window[:, channel] * np.hanning(len(window))))
        peaks.append(round(int(np.argmax(spectrum)) * 48_000 / len(window) / 10) * 10)
    return peaks[0], peaks[1]


class SwitchingAacTests(unittest.TestCase):
    """A stream recording declared mono whose frames run mono, stereo, then mono again."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        video._AUDIO_REPAIRS.clear()
        self.addCleanup(video._AUDIO_REPAIRS.clear)
        repair_dir = patch.object(video, "_AUDIO_REPAIR_DIR", self.root / "repaired")
        repair_dir.start()
        self.addCleanup(repair_dir.stop)

        parts = []
        for index, (left, right) in enumerate(((330, None), (550, 880), (440, None))):
            part = self.root / f"part{index}.aac"
            inputs = ["-f", "lavfi", "-i", f"sine=frequency={left}:sample_rate=48000:duration=2"]
            if right is None:
                layout = ["-ac", "1"]
            else:
                inputs += ["-f", "lavfi", "-i", f"sine=frequency={right}:sample_rate=48000:duration=2"]
                layout = ["-filter_complex", "[0:a][1:a]join=inputs=2:channel_layout=stereo[a]", "-map", "[a]"]
            _run([ffmpeg_executable(), "-y", "-v", "error", *inputs, *layout, "-c:a", "aac", "-f", "adts", str(part)])
            parts.append(part.read_bytes())
        broken = self.root / "broken.aac"
        broken.write_bytes(_mark_mono(b"".join(parts)))
        self.source = self.root / "vod.mp4"
        _run([
            ffmpeg_executable(), "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=gray:s=160x90:r=30:d=6", "-f", "aac", "-i", str(broken),
            "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "copy",
            "-shortest", str(self.source),
        ])

    def test_the_audio_is_repaired_once_and_decodes_the_same_every_time(self):
        repaired = audio_source(self.source)

        self.assertNotEqual(repaired, self.source)
        self.assertEqual(repaired.suffix, ".flac")
        self.assertIs(audio_source(self.source), repaired)
        digests = {hashlib.md5(_pcm(repaired).tobytes()).hexdigest() for _ in range(3)}
        self.assertEqual(len(digests), 1)

    def test_every_frame_is_heard_in_the_layout_it_was_made_in_at_its_own_time(self):
        samples = _pcm(audio_source(self.source))

        self.assertAlmostEqual(len(samples) / 48_000, probe_video(self.source).duration, delta=0.05)
        self.assertEqual(_tone(samples, 1.0), (330, 330))
        self.assertEqual(_tone(samples, 3.0), (550, 880))
        # FFmpeg alone keeps decoding this last mono part as if it were stereo.
        self.assertEqual(_tone(samples, 5.0), (440, 440))
        # Seeking lands on the same moment as in the video.
        self.assertEqual(_tone(_pcm(audio_source(self.source), start=4.0), 1.0), (440, 440))

    def test_exports_use_the_repaired_audio(self):
        clip = self.root / "clip.mp4"
        full = self.root / "full.mp4"
        export_clip(source=self.source, output=clip, start=4.0, end=6.0, aspect="9:16", resolution="720p")
        with patch.object(video, "_speech_pause_cut_intervals", return_value=[]):
            export_clip(
                source=self.source, output=full, start=0.0, end=6.0, aspect="16:9", resolution="720p",
                edit_mode="full-length", remove_silence=True, title_transcript=False, auto_sound_effect=False,
            )

        self.assertEqual(_tone(_pcm(clip), 1.0), (440, 440))
        self.assertEqual(_tone(_pcm(full), 3.0), (550, 880))
        self.assertEqual(_tone(_pcm(full), 5.0), (440, 440))

    def test_audio_that_keeps_its_layout_is_used_as_it_is(self):
        normal = self.root / "normal.mp4"
        _run([
            ffmpeg_executable(), "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=gray:s=160x90:r=30:d=2",
            "-f", "lavfi", "-i", "sine=frequency=500:sample_rate=48000:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(normal),
        ])
        wav = self.root / "speech.wav"
        _run([ffmpeg_executable(), "-y", "-v", "error", "-f", "lavfi", "-i", "sine=duration=1", str(wav)])

        self.assertEqual(audio_source(normal), normal)
        self.assertEqual(audio_source(wav), wav)

    def test_a_gap_in_the_recording_stays_a_gap(self):
        adts = self.root / "relabelled.aac"
        _run([ffmpeg_executable(), "-y", "-v", "error", "-i", str(self.source), "-map", "0:a", "-c:a", "copy",
              "-f", "adts", str(adts)])
        packets = video._packet_timestamps(ffmpeg_executable(), self.source, 48_000)
        # Pretend the recording dropped half a second before its 50th packet.
        packets = packets[:50] + [(start + 24_000, length) for start, length in packets[50:]]
        output = self.root / "gap.flac"

        video._decode_onto_timeline(ffmpeg_executable(), adts, output, 48_000, packets)

        samples = _pcm(output)
        gap_start = packets[50][0] - 24_000
        self.assertAlmostEqual(len(samples), packets[-1][0] + packets[-1][1], delta=1)
        self.assertFalse(np.any(samples[gap_start + 100:gap_start + 23_900]))
        self.assertTrue(np.any(samples[gap_start + 24_100:gap_start + 25_000]))


if __name__ == "__main__":
    unittest.main()
