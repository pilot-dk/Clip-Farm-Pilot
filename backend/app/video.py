from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Literal

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .brand import APP_SLUG, env
from .captions import (
    CaptionWord,
    DEFAULT_LIVE_CAPTION_HEIGHT,
    DEFAULT_LIVE_CAPTION_SCALE,
    LIVE_CAPTION_SCHEMES,
    LiveCaptionScheme,
    live_caption_font_size,
    detect_speech_segments,
    live_caption_margin,
    transcribe_words,
    write_live_caption_ass,
)

Aspect = Literal["16:9", "9:16", "1:1"]
Resolution = Literal["720p", "1080p", "2160p"]
FaceCorner = Literal["top-left", "top-right", "bottom-left", "bottom-right"]
CaptionPosition = Literal["top", "center", "bottom"]
SoundEffect = Literal["none", "vine-boom", "check-sound"]
SelectedSoundEffect = Literal["vine-boom", "check-sound"]
VisualEffect = Literal["none", "lens-flare", "punch-zoom", "white-flash"]
VideoFilter = Literal[
    "none",
    "black-white",
    "cinematic",
    "vivid",
    "warm",
    "cool",
    "faded",
    "high-contrast",
]

EFFECT_ASSETS_DIR = Path(__file__).resolve().parent / "assets"

ASPECT_SIZES: dict[Aspect, tuple[int, int]] = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
}

# Every layout is designed on the 1080p frame above. Other resolutions keep the
# same proportions and change only the short side of the frame.
DEFAULT_RESOLUTION: Resolution = "1080p"
REFERENCE_SHORT_SIDE = 1080
RESOLUTION_SHORT_SIDES: dict[Resolution, int] = {"720p": 720, "1080p": 1080, "2160p": 2160}

# Rates that cameras, capture cards, and streaming services record at, including
# their NTSC variants, so a rate read back as 29.97 becomes exactly 30000/1001.
_WHOLE_FRAME_RATES = (24, 25, 30, 48, 50, 60, 90, 100, 120, 144, 240)
_NTSC_FRAME_RATE_BASES = (24, 30, 48, 60, 120, 240)
_FRAME_RATE_TOLERANCE = Fraction(1, 200)


def output_size(aspect: Aspect, resolution: Resolution = DEFAULT_RESOLUTION) -> tuple[int, int]:
    """Pixel size of an export in the chosen shape and resolution."""
    if aspect not in ASPECT_SIZES:
        raise ValueError("Unknown aspect ratio.")
    if resolution not in RESOLUTION_SHORT_SIDES:
        raise ValueError("Unknown export resolution.")
    reference_width, reference_height = ASPECT_SIZES[aspect]
    short_side = RESOLUTION_SHORT_SIDES[resolution]
    return (
        reference_width * short_side // REFERENCE_SHORT_SIDE,
        reference_height * short_side // REFERENCE_SHORT_SIDE,
    )


def normalize_frame_rate(value: object) -> Fraction | None:
    """Turn a probed frame rate into an exact rate FFmpeg can reproduce."""
    if value is None:
        return None
    try:
        rate = value if isinstance(value, Fraction) else Fraction(str(value).strip())
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if rate <= 0 or rate > 480:
        return None
    for whole in _WHOLE_FRAME_RATES:
        if abs(rate - whole) <= _FRAME_RATE_TOLERANCE:
            return Fraction(whole)
    for base in _NTSC_FRAME_RATE_BASES:
        ntsc = Fraction(base * 1000, 1001)
        if abs(rate - ntsc) <= _FRAME_RATE_TOLERANCE:
            return ntsc
    return rate.limit_denominator(1001)


def format_frame_rate(rate: Fraction | None) -> str:
    """FFmpeg's rational spelling of a frame rate, such as 30000/1001."""
    if rate is None:
        return ""
    return str(rate.numerator) if rate.denominator == 1 else f"{rate.numerator}/{rate.denominator}"


def _frame_rate_args(rate: Fraction | None) -> list[str]:
    """Output options that keep the finished video at the source frame rate."""
    if rate is None:
        return []
    return ["-fps_mode", "cfr", "-r", format_frame_rate(rate)]


# Video encoders. A hardware H.264 encoder is used when this computer has one
# that works, and libx264 otherwise. Each hardware setting matches libx264
# -preset veryfast at the same CRF: on an M2 Max, VideoToolbox at quality 72
# scored the same VMAF as CRF 18 on stream footage (95.6 vs 95.3, 99.4 vs 98.8,
# 95.9 vs 95.7) at about the same bitrate, ran at ~375 fps where libx264 managed
# 190-360, and left most of the CPU free for the speech analysis.
EncoderSettings = Callable[[int, int, "Fraction | None", int], list[str]]


def _software_encode_args(crf: int) -> list[str]:
    return [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-profile:v", "high", "-pix_fmt", "yuv420p",
    ]


def _videotoolbox_quality_args(width: int, height: int, rate: Fraction | None, crf: int) -> list[str]:
    # Constant quality needs Apple silicon.
    return [
        "-c:v", "h264_videotoolbox", "-q:v", str(72 - 2 * (crf - 18)),
        "-profile:v", "high", "-g", "250", "-pix_fmt", "yuv420p",
    ]


def _videotoolbox_bitrate_args(width: int, height: int, rate: Fraction | None, crf: int) -> list[str]:
    # Intel Macs only offer a bitrate target, so it is set generously.
    frames_per_second = min(60.0, float(rate)) if rate else 30.0
    bitrate = int(width * height * frames_per_second * 0.16 * 0.85 ** (crf - 18))
    return [
        "-c:v", "h264_videotoolbox", "-b:v", str(bitrate), "-maxrate", str(bitrate * 3 // 2),
        "-bufsize", str(bitrate * 2), "-profile:v", "high", "-g", "250", "-pix_fmt", "yuv420p",
    ]


def _nvenc_args(width: int, height: int, rate: Fraction | None, crf: int) -> list[str]:
    return [
        "-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", str(crf),
        "-b:v", "0", "-spatial-aq", "1", "-profile:v", "high", "-g", "250", "-pix_fmt", "yuv420p",
    ]


def _quick_sync_args(width: int, height: int, rate: Fraction | None, crf: int) -> list[str]:
    return [
        "-c:v", "h264_qsv", "-preset", "medium", "-global_quality", str(crf + 1),
        "-profile:v", "high", "-g", "250", "-pix_fmt", "nv12",
    ]


def _amf_args(width: int, height: int, rate: Fraction | None, crf: int) -> list[str]:
    return [
        "-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp",
        "-qp_i", str(crf), "-qp_p", str(crf + 2), "-profile:v", "high", "-g", "250", "-pix_fmt", "yuv420p",
    ]


_HARDWARE_ENCODERS: dict[str, tuple[tuple[str, EncoderSettings], ...]] = {
    "darwin": (("VideoToolbox", _videotoolbox_quality_args), ("VideoToolbox", _videotoolbox_bitrate_args)),
    "win32": (("NVENC", _nvenc_args), ("Quick Sync", _quick_sync_args), ("AMF", _amf_args)),
    "linux": (("NVENC", _nvenc_args),),
}
_HARDWARE_ENCODER_CHOICES: dict[str, tuple[str, EncoderSettings] | None] = {}
_HARDWARE_ENCODER_LOCK = threading.Lock()


def _encoder_works(ffmpeg: str, codec_args: list[str]) -> bool:
    """Encode one second of a test pattern to see whether an encoder runs here."""
    with tempfile.TemporaryDirectory(prefix=f"{APP_SLUG}-encoder-") as folder:
        probe = Path(folder) / "probe.mp4"
        try:
            result = subprocess.run(
                [
                    ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=1",
                    *codec_args, "-frames:v", "30", str(probe),
                ],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0 and probe.is_file() and probe.stat().st_size > 1_000


def _hardware_encoder(ffmpeg: str) -> tuple[str, EncoderSettings] | None:
    """The first hardware encoder that works with this FFmpeg, checked once a session."""
    if str(env("VIDEO_ENCODER", "auto")).strip().lower() in {"software", "cpu", "libx264"}:
        return None
    platform = "win32" if sys.platform.startswith("win") else "darwin" if sys.platform == "darwin" else "linux"
    with _HARDWARE_ENCODER_LOCK:
        if ffmpeg not in _HARDWARE_ENCODER_CHOICES:
            _HARDWARE_ENCODER_CHOICES[ffmpeg] = next(
                (
                    (name, settings)
                    for name, settings in _HARDWARE_ENCODERS.get(platform, ())
                    if _encoder_works(ffmpeg, settings(1280, 720, Fraction(30), 18))
                ),
                None,
            )
        return _HARDWARE_ENCODER_CHOICES[ffmpeg]


def _encode_video(
    command_for: Callable[[list[str]], list[str]],
    width: int,
    height: int,
    frame_rate: Fraction | None,
    crf: int,
) -> str:
    """Run a video encode on the hardware encoder when there is one, else libx264.

    command_for turns encoder options into the full FFmpeg command. Returns the
    name of the encoder that made the file.
    """
    software = command_for(_software_encode_args(crf))
    hardware = _hardware_encoder(software[0])
    if hardware is not None:
        name, settings = hardware
        try:
            _run(command_for(settings(width, height, frame_rate, crf)))
            return name
        except subprocess.CalledProcessError:
            # A GPU can still turn down a job the probe passed, such as a frame
            # size it cannot encode. libx264 handles anything, so export anyway.
            pass
    _run(software)
    return "libx264"


VIDEO_FILTER_CHAINS: dict[VideoFilter, str] = {
    "none": "",
    "black-white": "hue=s=0,eq=contrast=1.12:brightness=0.01",
    "cinematic": (
        "eq=contrast=1.16:saturation=0.88:brightness=-0.025,"
        "colorbalance=rs=-0.035:gs=0.01:bs=0.075:rm=0.045:gm=0.005:bm=-0.04:pl=1"
    ),
    "vivid": "eq=contrast=1.10:saturation=1.35:brightness=0.015",
    "warm": "colorbalance=rs=0.08:rm=0.055:rh=0.035:bs=-0.07:bm=-0.04:pl=1,eq=saturation=1.08",
    "cool": "colorbalance=rs=-0.055:rm=-0.035:bs=0.08:bm=0.055:bh=0.03:pl=1,eq=saturation=1.04",
    "faded": "eq=contrast=0.84:saturation=0.78:brightness=0.055,colorbalance=rs=0.025:bs=0.045:pl=1",
    "high-contrast": "eq=contrast=1.32:saturation=1.08:brightness=-0.015",
}


@dataclass
class VideoInfo:
    width: int
    height: int
    duration: float
    frame_rate: Fraction | None = None


@dataclass
class AudioAnalysis:
    """One-second audio features used by the clip-ranking pipeline."""

    rms: np.ndarray
    peak: np.ndarray
    burst: np.ndarray
    texture: np.ndarray


_AUDIO_ANALYSIS_CACHE: dict[tuple[str, int, int, int], AudioAnalysis] = {}
_AUDIO_ANALYSIS_CACHE_LOCK = threading.RLock()
_AUDIO_ANALYSIS_CACHE_LIMIT = 4
_FFMPEG_ASS_SUPPORT: dict[str, bool] = {}
_TRANSCRIPT_CACHE: dict[tuple[object, ...], tuple[CaptionWord, ...]] = {}
_TRANSCRIPT_CACHE_LOCK = threading.RLock()
_TRANSCRIPT_CACHE_LIMIT = 6
_SILENCE_CUT_CACHE: dict[tuple[object, ...], tuple[tuple[float, float], ...]] = {}
_CLIP_ENVELOPE_CACHE: dict[tuple[object, ...], tuple[np.ndarray, float]] = {}
_SCENE_CHANGE_CACHE: dict[tuple[object, ...], tuple[float, ...]] = {}
_FULL_LENGTH_ANALYSIS_CACHE_LOCK = threading.RLock()
_FULL_LENGTH_ANALYSIS_CACHE_LIMIT = 6


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _ffmpeg_supports_ass(executable: str) -> bool:
    if executable in _FFMPEG_ASS_SUPPORT:
        return _FFMPEG_ASS_SUPPORT[executable]
    result = subprocess.run(
        [executable, "-hide_banner", "-filters"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    filters = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    supported = bool(re.search(r"\b(?:ass|subtitles)\s+V->V\b", filters))
    _FFMPEG_ASS_SUPPORT[executable] = supported
    return supported


def ffmpeg_executable(require_ass: bool = False) -> str:
    configured = env("FFMPEG_EXE")
    if configured and Path(configured).exists() and (not require_ass or _ffmpeg_supports_ass(str(configured))):
        return configured

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg and (not require_ass or _ffmpeg_supports_ass(system_ffmpeg)):
        return system_ffmpeg

    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if require_ass and not _ffmpeg_supports_ass(bundled):
            raise RuntimeError("The bundled video engine does not support live captions.")
        return bundled
    except Exception as exc:
        raise RuntimeError("FFmpeg is not available.") from exc


def probe_video(path: Path) -> VideoInfo:
    configured_probe = env("FFPROBE_EXE")
    ffprobe = configured_probe if configured_probe and Path(configured_probe).exists() else shutil.which("ffprobe")
    if ffprobe:
        result = _run([
            ffprobe, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate:format=duration",
            "-of", "json",
            str(path),
        ])
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        duration = float(data.get("format", {}).get("duration", 0) or 0)
        return VideoInfo(
            width=int(stream["width"]),
            height=int(stream["height"]),
            duration=duration,
            frame_rate=_probed_frame_rate(stream.get("r_frame_rate"), stream.get("avg_frame_rate")),
        )

    # The desktop bundle carries imageio-ffmpeg's standalone FFmpeg binary, so
    # probing still works even when Homebrew and ffprobe are not installed.
    import imageio_ffmpeg

    reader = imageio_ffmpeg.read_frames(str(path), pix_fmt="rgb24")
    try:
        metadata = next(reader)
    finally:
        reader.close()
    width, height = metadata["size"]
    duration = float(metadata.get("duration") or 0)
    return VideoInfo(
        width=int(width),
        height=int(height),
        duration=duration,
        frame_rate=normalize_frame_rate(metadata.get("fps")),
    )


def _probed_frame_rate(nominal: object, average: object) -> Fraction | None:
    """Pick the rate a constant- or variable-rate recording actually plays at."""
    nominal_rate = normalize_frame_rate(nominal)
    average_rate = normalize_frame_rate(average)
    if nominal_rate and average_rate:
        # Variable-rate phone and capture files report a high timebase as the
        # nominal rate; their average is the rate a viewer actually sees.
        if abs(nominal_rate - average_rate) / nominal_rate <= Fraction(1, 100):
            return nominal_rate
        return average_rate
    return nominal_rate or average_rate


def _read_exactly(stream, byte_count: int) -> bytes:
    """Read up to byte_count bytes without assuming one pipe read is complete."""
    chunks: list[bytes] = []
    remaining = byte_count
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _audio_analysis_per_second(path: Path, sample_rate: int = 8000) -> AudioAnalysis:
    """Decode a VOD as a stream so multi-hour recordings do not fill RAM.

    Besides loudness, each second records the near-peak level, short reaction
    bursts, and high-frequency texture. Those signals distinguish a sustained
    crowd/creator reaction from one isolated click or a uniformly loud soundtrack.
    """
    resolved = Path(path).resolve()
    stat = resolved.stat()
    cache_key = (str(resolved), int(stat.st_size), int(stat.st_mtime_ns), int(sample_rate))
    with _AUDIO_ANALYSIS_CACHE_LOCK:
        cached = _AUDIO_ANALYSIS_CACHE.get(cache_key)
        if cached is not None:
            return cached

    command = [
        ffmpeg_executable(), "-v", "error", "-i", str(audio_source(resolved)),
        "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-f", "s16le", "pipe:1",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if process.stdout is None:
        raise RuntimeError("FFmpeg did not provide decoded audio.")

    rms_values: list[float] = []
    peak_values: list[float] = []
    burst_values: list[float] = []
    texture_values: list[float] = []
    bytes_per_second = sample_rate * np.dtype(np.int16).itemsize

    try:
        while True:
            raw = _read_exactly(process.stdout, bytes_per_second)
            if not raw:
                break
            usable = len(raw) - (len(raw) % 2)
            samples = np.frombuffer(raw[:usable], dtype=np.int16).astype(np.float32) / 32768.0
            if samples.size == 0:
                continue

            rms = float(np.sqrt(np.mean(samples * samples) + 1e-12))
            peak = float(np.percentile(np.abs(samples), 97))
            subframes = np.array_split(samples, min(10, max(1, samples.size)))
            sub_rms = np.asarray([
                float(np.sqrt(np.mean(frame * frame) + 1e-12))
                for frame in subframes
                if frame.size
            ], dtype=np.float32)
            burst = float(max(0.0, np.max(sub_rms, initial=0.0) - np.median(sub_rms)))
            texture = float(np.mean(np.abs(np.diff(samples)))) if samples.size > 1 else 0.0

            rms_values.append(rms)
            peak_values.append(peak)
            burst_values.append(burst)
            texture_values.append(texture)
            if len(raw) < bytes_per_second:
                break
    finally:
        process.stdout.close()
        return_code = process.wait()

    if return_code != 0 and not rms_values:
        raise RuntimeError("The VOD audio track could not be decoded.")

    analysis = AudioAnalysis(
        rms=np.asarray(rms_values or [0.0], dtype=np.float32),
        peak=np.asarray(peak_values or [0.0], dtype=np.float32),
        burst=np.asarray(burst_values or [0.0], dtype=np.float32),
        texture=np.asarray(texture_values or [0.0], dtype=np.float32),
    )
    with _AUDIO_ANALYSIS_CACHE_LOCK:
        _AUDIO_ANALYSIS_CACHE[cache_key] = analysis
        while len(_AUDIO_ANALYSIS_CACHE) > _AUDIO_ANALYSIS_CACHE_LIMIT:
            _AUDIO_ANALYSIS_CACHE.pop(next(iter(_AUDIO_ANALYSIS_CACHE)))
    return analysis


def _audio_rms_per_second(path: Path, sample_rate: int = 8000) -> np.ndarray:
    """Compatibility wrapper shared with the viral-title generator."""
    return _audio_analysis_per_second(path, sample_rate=sample_rate).rms


def _robust_unit(values: np.ndarray, low_percentile: float = 15, high_percentile: float = 95) -> np.ndarray:
    """Map a noisy signal to 0..1 without letting one outlier dominate it."""
    samples = np.asarray(values, dtype=np.float32)
    if samples.size == 0:
        return np.zeros(0, dtype=np.float32)
    finite = samples[np.isfinite(samples)]
    if finite.size == 0:
        return np.zeros_like(samples)
    low = float(np.percentile(finite, low_percentile))
    high = float(np.percentile(finite, high_percentile))
    if high - low < 1e-7:
        # Sparse reactions may occupy less than the chosen upper percentile.
        # Fall back to the real maximum instead of erasing a valid short spike.
        high = float(np.max(finite, initial=low))
        if high - low < 1e-7:
            return np.zeros_like(samples)
    return np.clip((np.nan_to_num(samples, nan=low) - low) / (high - low), 0, 1)


def _moving_average(values: np.ndarray, width: int) -> np.ndarray:
    if values.size == 0 or width <= 1:
        return values.astype(np.float32, copy=True)
    width = min(values.size, max(1, int(width)))
    kernel = np.ones(width, dtype=np.float32) / width
    return np.convolve(values, kernel, mode="same").astype(np.float32)


def _top_mean(values: np.ndarray, count: int = 3) -> float:
    if values.size == 0:
        return 0.0
    count = min(max(1, count), values.size)
    return float(np.mean(np.partition(values, values.size - count)[-count:]))


def _prepare_audio_signals(audio: AudioAnalysis, duration: float) -> dict[str, np.ndarray]:
    length = max(1, int(math.ceil(duration)))

    def sized(values: np.ndarray) -> np.ndarray:
        result = np.zeros(length, dtype=np.float32)
        copied = min(length, values.size)
        if copied:
            result[:copied] = np.nan_to_num(values[:copied], nan=0.0)
        return result

    raw_rms = sized(audio.rms)
    energy = _robust_unit(raw_rms, 10, 96)
    peak = _robust_unit(sized(audio.peak), 15, 97)
    burst = _robust_unit(sized(audio.burst), 20, 97)
    texture_ratio = sized(audio.texture) / np.maximum(raw_rms, 1e-4)
    texture = _robust_unit(np.clip(texture_ratio, 0, 4), 15, 95)

    rise = _robust_unit(np.clip(np.diff(energy, prepend=energy[0]), 0, None), 45, 98)
    fast_energy = _moving_average(energy, 3)
    slow_energy = _moving_average(energy, 17)
    contrast = _robust_unit(np.clip(fast_energy - slow_energy, 0, None), 35, 97)
    momentum = np.clip(0.55 * fast_energy + 0.25 * peak + 0.20 * burst, 0, 1)
    salience = np.clip(
        0.28 * energy
        + 0.22 * rise
        + 0.16 * contrast
        + 0.14 * burst
        + 0.12 * peak
        + 0.08 * texture,
        0,
        1,
    )
    salience = np.clip(0.72 * salience + 0.28 * _moving_average(salience, 3), 0, 1)
    return {
        "raw_rms": raw_rms,
        "energy": energy,
        "peak": peak,
        "burst": burst,
        "texture": texture,
        "rise": rise,
        "contrast": contrast,
        "momentum": momentum,
        "salience": salience,
    }


def _window_candidate(signals: dict[str, np.ndarray], peak_second: int, duration: float, window: int) -> dict:
    latest_start = max(0.0, duration - window)
    # Place the payoff late enough to preserve setup/context and still keep the reaction.
    start = min(latest_start, max(0.0, float(peak_second) - window * 0.68))
    end = min(duration, start + window)
    start_index = int(math.floor(start))
    end_index = max(start_index + 1, min(len(signals["salience"]), int(math.ceil(end))))

    def segment(name: str) -> np.ndarray:
        return signals[name][start_index:end_index]

    energy = segment("energy")
    salience = segment("salience")
    rise = segment("rise")
    burst = segment("burst")
    contrast = segment("contrast")
    momentum = segment("momentum")
    peak = segment("peak")

    third = max(1, energy.size // 3)
    early = float(np.mean(energy[:third]))
    late = float(np.mean(energy[-third:]))
    escalation = float(np.clip((late - early + 0.2) / 0.75, 0, 1))
    reaction = np.clip(0.58 * _top_mean(rise, 2) + 0.42 * _top_mean(burst, 3), 0, 1)
    sustained = np.clip(0.62 * float(np.mean(momentum)) + 0.38 * _top_mean(peak, 5), 0, 1)
    contrast_score = np.clip(0.7 * _top_mean(contrast, 4) + 0.3 * _top_mean(salience, 3), 0, 1)
    dead_air = float(np.mean(energy < 0.06))
    payoff_position = (peak_second - start) / max(1.0, end - start)
    payoff_fit = float(math.exp(-((payoff_position - 0.68) / 0.23) ** 2))

    audio_score = (
        0.27 * _top_mean(salience, 4)
        + 0.20 * reaction
        + 0.17 * sustained
        + 0.14 * contrast_score
        + 0.12 * escalation
        + 0.10 * payoff_fit
    )
    audio_score -= min(0.18, max(0.0, dead_air - 0.55) * 0.4)
    audio_score = float(np.clip(audio_score, 0, 1))
    return {
        "start": round(float(start), 2),
        "end": round(float(end), 2),
        "peak": round(float(peak_second), 2),
        "audio_score": audio_score,
        "reaction": float(reaction),
        "momentum": float(sustained),
        "contrast": float(contrast_score),
        "escalation": float(escalation),
        "dead_air": dead_air,
        "visual": 0.0,
        "visual_cuts": 0.0,
    }


def _candidate_overlap(first: dict, second: dict) -> float:
    overlap = max(0.0, min(first["end"], second["end"]) - max(first["start"], second["start"]))
    union = max(first["end"], second["end"]) - min(first["start"], second["start"])
    return overlap / max(union, 1e-6)


def _preliminary_candidates(
    signals: dict[str, np.ndarray],
    duration: float,
    target_duration: int,
    limit: int,
) -> list[dict]:
    window = max(8, min(int(target_duration), max(8, int(math.ceil(duration)))))
    salience = signals["salience"]
    if float(np.max(salience, initial=0.0)) < 0.04:
        # With no useful audio, spread visual probes across the entire VOD rather
        # than accidentally sampling only its ending.
        sample_count = min(max(12, limit * 4), max(1, int(math.ceil(duration / window))))
        local_maxima = [
            min(salience.size - 1, max(0, int(round(value))))
            for value in np.linspace(window * 0.68, max(window * 0.68, duration - 1), sample_count)
        ]
    else:
        minimum_salience = max(0.04, float(np.max(salience, initial=0.0)) * 0.12)
        local_maxima = [
            index for index in range(salience.size)
            if salience[index] >= minimum_salience
            and salience[index] >= salience[max(0, index - 2):min(salience.size, index + 3)].max(initial=0.0)
        ]
    if not local_maxima:
        local_maxima = list(range(salience.size))
    local_maxima = list(dict.fromkeys(local_maxima))
    local_maxima.sort(key=lambda index: (float(salience[index]), index), reverse=True)

    pool_size = max(12, limit * 4)
    selected: list[dict] = []
    for peak_second in local_maxima:
        candidate = _window_candidate(signals, peak_second, duration, window)
        if candidate["end"] - candidate["start"] < min(5.0, duration):
            continue
        if any(
            _candidate_overlap(candidate, existing) > 0.52
            or abs(candidate["peak"] - existing["peak"]) < window * 0.38
            for existing in selected
        ):
            continue
        selected.append(candidate)
        if len(selected) >= pool_size:
            break

    if not selected:
        fallback_peak = min(max(0, window // 2), max(0, int(duration) - 1))
        selected = [_window_candidate(signals, fallback_peak, duration, window)]
    selected.sort(key=lambda candidate: candidate["audio_score"], reverse=True)
    return selected


def _visual_window_summary(path: Path, start: float, end: float, fps: float = 2.0) -> tuple[float, float]:
    """Measure action and hard visual changes only inside a shortlisted window."""
    width, height = 64, 36
    clip_duration = max(0.5, end - start)
    command = [
        ffmpeg_executable(), "-v", "error", "-ss", f"{start:.3f}", "-t", f"{clip_duration:.3f}",
        "-i", str(path), "-an",
        "-vf", f"fps={fps},scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
        "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1",
    ]
    process = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    frame_size = width * height
    frame_count = len(process.stdout) // frame_size
    if frame_count < 2:
        return 0.0, 0.0
    frames = np.frombuffer(process.stdout[:frame_count * frame_size], dtype=np.uint8)
    frames = frames.reshape(frame_count, frame_size).astype(np.float32) / 255.0
    differences = np.mean(np.abs(np.diff(frames, axis=0)), axis=1)
    if differences.size == 0:
        return 0.0, 0.0
    motion = float(np.clip(0.55 * np.mean(differences) / 0.12 + 0.45 * np.percentile(differences, 85) / 0.24, 0, 1))
    cuts = float(np.clip(np.mean(differences > 0.18) / 0.22, 0, 1))
    return motion, cuts


def _explain_candidate(candidate: dict) -> tuple[str, str]:
    reaction = candidate["reaction"]
    visual = candidate["visual"]
    cuts = candidate["visual_cuts"]
    momentum = candidate["momentum"]
    escalation = candidate["escalation"]
    contrast = candidate["contrast"]
    if reaction >= 0.62 and visual >= 0.52:
        return "Reaction + payoff", "Sudden reaction with strong visual action"
    if reaction >= 0.68:
        return "Big reaction", "Sharp audio reaction with a clear payoff"
    if escalation >= 0.66 and contrast >= 0.5:
        return "Build-up", "Energy builds into a strong ending"
    if visual >= 0.67 or cuts >= 0.72:
        return "Fast action", "Strong motion and visual changes"
    if momentum >= 0.62:
        return "High intensity", "Sustained energy across the full moment"
    if contrast >= 0.58:
        return "Standout moment", "Clearly stronger than the surrounding VOD"
    return "Promising moment", "Best combined audio and visual signal in this section"


def _finalize_candidates(candidates: list[dict], target_duration: int, limit: int) -> list[dict]:
    if not candidates:
        return []
    qualities = np.asarray([candidate["quality"] for candidate in candidates], dtype=np.float32)
    order = np.argsort(qualities)
    ranks = np.empty(len(candidates), dtype=np.float32)
    ranks[order] = (
        np.ones(1, dtype=np.float32)
        if len(candidates) == 1
        else np.linspace(0.0, 1.0, len(candidates), dtype=np.float32)
    )
    for index, candidate in enumerate(candidates):
        confidence = float(np.clip(0.78 * candidate["quality"] + 0.22 * ranks[index], 0, 1))
        candidate["score"] = round(float(np.clip(28 + 70 * confidence, 1, 98)), 1)
        candidate["label"], candidate["reason"] = _explain_candidate(candidate)
        candidate["signals"] = {
            "reaction": round(candidate["reaction"] * 100),
            "momentum": round(candidate["momentum"] * 100),
            "visual": round(candidate["visual"] * 100),
            "contrast": round(candidate["contrast"] * 100),
        }

    candidates.sort(key=lambda candidate: (candidate["score"], candidate["quality"]), reverse=True)
    picks: list[dict] = []
    for candidate in candidates:
        if any(
            _candidate_overlap(candidate, existing) > 0.34
            or abs(candidate["peak"] - existing["peak"]) < max(5.0, target_duration * 0.48)
            for existing in picks
        ):
            continue
        picks.append({
            key: candidate[key]
            for key in ("start", "end", "peak", "score", "label", "reason", "signals")
        })
        if len(picks) >= limit:
            break
    return picks


def analyze_viral_candidates(path: Path, target_duration: int = 30, limit: int = 5) -> list[dict]:
    """Rank clip-worthy moments using reactions, momentum, contrast, and visuals.

    The full VOD gets a streaming audio pass that is safe for multi-hour files.
    Only a diverse shortlist is decoded visually, avoiding an expensive frame-by-
    frame scan of the entire recording. Results preserve setup before each likely
    payoff, reject dead-air-heavy windows, and explain why every clip was picked.
    """
    info = probe_video(path)
    if info.duration <= 0:
        return []
    requested_duration = max(8, min(int(target_duration), 90))
    requested_limit = max(1, min(int(limit), 10))

    try:
        audio = _audio_analysis_per_second(path)
    except (OSError, subprocess.SubprocessError, RuntimeError):
        empty = np.zeros(max(1, int(math.ceil(info.duration))), dtype=np.float32)
        audio = AudioAnalysis(empty, empty.copy(), empty.copy(), empty.copy())
    signals = _prepare_audio_signals(audio, info.duration)
    candidates = _preliminary_candidates(signals, info.duration, requested_duration, requested_limit)
    has_meaningful_audio = float(np.max(signals["raw_rms"], initial=0.0)) >= 1e-5

    # Visual analysis is deliberately restricted to the strongest diverse shortlist.
    visual_probe_count = len(candidates) if not has_meaningful_audio else max(10, requested_limit * 2)
    for candidate in candidates[:visual_probe_count]:
        try:
            motion, cuts = _visual_window_summary(path, candidate["start"], candidate["end"])
        except (OSError, subprocess.SubprocessError, RuntimeError):
            motion, cuts = 0.0, 0.0
        candidate["visual"] = motion
        candidate["visual_cuts"] = cuts

    for candidate in candidates:
        visual_score = np.clip(0.72 * candidate["visual"] + 0.28 * candidate["visual_cuts"], 0, 1)
        audio_score = candidate["audio_score"]
        candidate["quality"] = float(np.clip(
            (0.80 * audio_score + 0.20 * visual_score) if has_meaningful_audio
            else visual_score,
            0,
            1,
        ))

    picks = _finalize_candidates(candidates, requested_duration, requested_limit)
    if not picks:
        end = min(float(requested_duration), info.duration)
        return [{
            "start": 0.0,
            "end": end,
            "peak": round(end * 0.68, 2),
            "score": 35.0,
            "label": "Opening moment",
            "reason": "Not enough variation was found to rank distinct moments",
            "signals": {"reaction": 0, "momentum": 0, "visual": 0, "contrast": 0},
        }]
    return picks


def _clean_title_context(raw_title: str, limit: int = 48) -> str:
    """Turn a VOD title or uploaded filename into a compact title subject."""
    value = unicodedata.normalize("NFKC", str(raw_title or ""))
    value = Path(value).stem.replace("_", " ")
    value = re.sub(r"[\[(](?:full\s*)?(?:stream|livestream|vod|video)[^\])]*[\])]", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(?:full\s+)?(?:twitch|youtube)?\s*(?:stream|livestream|vod)\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*(?:\||[-–—])\s*(?:twitch|youtube)\s*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip(" ._-–—|")
    if re.fullmatch(r"[0-9a-f]{16,}", value, flags=re.IGNORECASE):
        return ""
    if len(value) > limit:
        shortened = value[: limit + 1].rsplit(" ", 1)[0].strip()
        value = shortened or value[:limit].strip()
    return value


def _classify_clip_energy(rms: np.ndarray) -> tuple[str, str]:
    """Classify the shape of a clip's excitement without claiming semantics."""
    samples = np.asarray(rms, dtype=np.float32)
    samples = samples[np.isfinite(samples)]
    if samples.size < 3 or float(np.max(np.abs(samples), initial=0.0)) < 1e-6:
        return "surprise", "The Moment I Didn’t See Coming"

    lo = float(np.percentile(samples, 10))
    hi = float(np.percentile(samples, 95))
    norm = np.clip((samples - lo) / max(hi - lo, 1e-6), 0, 1)
    third = max(1, len(norm) // 3)
    first = float(np.mean(norm[:third]))
    last = float(np.mean(norm[-third:]))
    peak_position = int(np.argmax(norm)) / max(1, len(norm) - 1)
    positive_spikes = np.clip(np.diff(norm, prepend=norm[0]), 0, 1)
    strongest_spike = float(np.max(positive_spikes, initial=0.0))

    if peak_position >= 0.62 and last >= first + 0.08:
        return "big_finish", "Wait for the Ending"
    if peak_position <= 0.32 and first >= last + 0.08:
        return "fast_start", "It Started With Chaos"
    if last >= first + 0.18 or strongest_spike >= 0.55:
        return "escalation", "This Escalated Fast"
    if float(np.mean(norm)) >= 0.55:
        return "sustained", "The Most Intense Moment"
    return "surprise", "The Moment I Didn’t See Coming"


def safe_export_filename(title: str, fallback: str = "Clip Farm Pilot Viral Moment") -> str:
    """Create a portable MP4 filename while keeping readable Unicode and emoji."""
    stem = unicodedata.normalize("NFKC", str(title or ""))
    stem = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" .")
    if not stem:
        stem = fallback
    if len(stem) > 96:
        shortened = stem[:97].rsplit(" ", 1)[0].strip(" .")
        stem = shortened or stem[:96].strip(" .")
    return f"{stem}.mp4"


_TITLE_EDGE_WORDS = {
    "a", "an", "and", "are", "as", "at", "because", "but", "for", "from", "in", "is",
    "it", "like", "of", "on", "or", "so", "that", "the", "then", "this", "to", "uh",
    "um", "was", "well", "were", "with", "you", "your",
}
_TITLE_FILLER_STARTS = {
    "actually", "and", "basically", "but", "honestly", "like", "okay", "right", "seriously",
    "so", "uh", "um", "well", "yeah",
}
_TITLE_SIGNAL_WORDS = {
    "best", "broke", "changed", "craziest", "crazy", "finally", "first", "goal", "impossible",
    "insane", "last", "never", "secret", "shocked", "truth", "unexpected", "wild", "win", "won", "worst",
}


def _title_case_phrase(value: str) -> str:
    """Title-case ordinary words without breaking apostrophes, acronyms, or emoji."""
    small_words = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to"}
    tokens = value.split()
    result: list[str] = []
    for index, token in enumerate(tokens):
        match = re.match(r"^([^\w]*)([\w'’.-]+)([^\w]*)$", token, flags=re.UNICODE)
        if not match:
            result.append(token)
            continue
        prefix, word, suffix = match.groups()
        lowered = word.lower()
        if word.isupper() and len(word) <= 5:
            rendered = word
        elif 0 < index < len(tokens) - 1 and lowered in small_words:
            rendered = lowered
        else:
            rendered = lowered[:1].upper() + lowered[1:]
        result.append(f"{prefix}{rendered}{suffix}")
    return " ".join(result)


def _clean_title_subject(value: str, word_limit: int = 9, character_limit: int = 58) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = re.sub(r"\[(?:music|applause|laughter|noise)\]", " ", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"[/|]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip(" \t\r\n,.;:!?—–-\"“”")
    words = normalized.split()
    while words and words[0].lower().strip(".,!?") in _TITLE_FILLER_STARTS:
        words.pop(0)
    superlatives = {"best", "biggest", "craziest", "fastest", "greatest", "wildest", "worst"}
    for index, word in enumerate(words):
        if word.lower().strip(".,!?") in superlatives and index > 0:
            earlier = {item.lower().strip(".,!?") for item in words[:index]}
            if words[index - 1].lower().strip(".,!?") == "the":
                words = words[index - 1:]
            elif earlier <= {"actually", "i", "is", "it", "that", "that's", "this", "was"}:
                words = words[index:]
            break
    words = words[:word_limit]
    while len(words) > 2 and words[-1].lower().strip(".,!?") in _TITLE_EDGE_WORDS:
        words.pop()
    subject = " ".join(words).strip(" ,.;:!?—–-\"“”")
    if len(subject) > character_limit:
        subject = subject[: character_limit + 1].rsplit(" ", 1)[0].strip(" ,.;:!?—–-")
    rendered = _title_case_phrase(subject)
    first_word = re.sub(r"[^\w]", "", rendered.split()[0]).casefold() if rendered else ""
    if first_word in superlatives:
        rendered = f"The {rendered}"
    return rendered


def _transcript_title_subject(transcript_text: str) -> str:
    """Choose a short, concrete phrase from the actual words in the exported clip."""
    normalized = unicodedata.normalize("NFKC", str(transcript_text or ""))
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return ""
    chunks = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\s*[;:]\s*", normalized) if part.strip()]
    candidates: list[tuple[float, str]] = []
    for chunk_index, chunk in enumerate(chunks):
        raw_words = chunk.split()
        windows: list[tuple[list[str], int]] = []
        if len(raw_words) <= 10:
            windows.append((raw_words, 0))
        else:
            windows.extend(((raw_words[:9], 0), (raw_words[-9:], max(0, len(raw_words) - 9))))
            for start in range(3, max(4, len(raw_words) - 5), 5):
                windows.append((raw_words[start:start + 9], start))
        for window, window_start in windows:
            subject = _clean_title_subject(" ".join(window))
            words = re.findall(r"[\w'’]+", subject.lower(), flags=re.UNICODE)
            meaningful = [word for word in words if word not in _TITLE_EDGE_WORDS]
            if len(words) < 3 or len(meaningful) < 2:
                continue
            generic = subject.casefold().strip(" .!?") in {
                "thank you", "like and subscribe", "subscribe to the channel",
            }
            if generic:
                continue
            signal_bonus = sum(word in _TITLE_SIGNAL_WORDS for word in meaningful) * 2.4
            diversity = len(set(meaningful)) / max(1, len(meaningful))
            length_fit = 2.0 - abs(len(words) - 6) * 0.25
            punctuation_bonus = 0.45 if re.search(r"[!?]", chunk) else 0.0
            position_bonus = chunk_index / max(1, len(chunks) - 1) * 0.20
            opening_bonus = 0.70 if window_start == 0 else 0.0
            connective_penalty = 1.25 if any(word in {"because", "although", "unless", "while"} for word in words[1:-1]) else 0.0
            score = len(meaningful) * 0.34 + signal_bonus + diversity + length_fit + punctuation_bonus + position_bonus + opening_bonus - connective_penalty
            candidates.append((score, subject))
    if not candidates:
        generic_chunks = {"thank you", "like and subscribe", "subscribe to the channel"}
        if all(_clean_title_subject(chunk).casefold().strip(" .!?") in generic_chunks for chunk in chunks):
            return ""
        return _clean_title_subject(normalized)
    return max(candidates, key=lambda item: (item[0], len(item[1])))[1]


_TITLE_TEMPLATES: dict[str, tuple[str, ...]] = {
    "big_finish": (
        "{subject} — Wait for the Payoff",
        "How {subject} Built to That Ending",
        "{subject} Was Only the Beginning",
        "The Payoff After {subject}",
        "{subject}: The Ending Made It Worth It",
        "Everything Changed After {subject}",
        "The Final Seconds of {subject}",
        "{subject} — It All Comes Down to This",
        "What Happened After {subject}",
        "{subject}: One Last Twist",
        "The Ending Nobody Saw in {subject}",
        "{subject} Saved Its Best Moment for Last",
    ),
    "fast_start": (
        "{subject} and Instant Chaos",
        "{subject}: Zero to Chaos in Seconds",
        "How {subject} Kicked Everything Off",
        "{subject} Started at Full Speed",
        "No Warm-Up — Just {subject}",
        "{subject} Went Off Immediately",
        "The Fastest Start to {subject}",
        "{subject}: It Got Wild Instantly",
        "From the First Second: {subject}",
        "{subject} Did Not Waste Any Time",
        "The Chaos Started With {subject}",
        "{subject} Hit Different From the Start",
    ),
    "escalation": (
        "How {subject} Escalated So Fast",
        "{subject} — Then It Got Even Wilder",
        "The Moment {subject} Went Off the Rails",
        "{subject}: A Normal Moment Until It Wasn’t",
        "How Did {subject} Turn Into This?",
        "{subject} Kept Getting More Intense",
        "The Exact Second {subject} Changed",
        "{subject} Took a Wild Turn",
        "It Started With {subject} and Kept Going",
        "{subject}: This Escalated Quickly",
        "The Build-Up to {subject} Was Unreal",
        "{subject} Got Wilder by the Second",
    ),
    "sustained": (
        "{subject} Was Pure Intensity",
        "The Most Intense Part of {subject}",
        "{subject}: No Breaks, Just Chaos",
        "Why {subject} Had Everyone Locked In",
        "{subject} Never Let Up",
        "Every Second of {subject} Mattered",
        "{subject} at Maximum Intensity",
        "The Pressure Never Dropped During {subject}",
        "{subject}: The Clip That Never Slowed Down",
        "Inside the Wildest Part of {subject}",
        "{subject} Was Nonstop",
        "The Energy Around {subject} Was Different",
    ),
    "surprise": (
        "{subject} — I Did Not See That Coming",
        "The Twist After {subject}",
        "{subject}: That Took a Turn",
        "What Just Happened With {subject}?",
        "{subject} Changed in One Second",
        "The Unexpected Side of {subject}",
        "{subject} Was Not Going How I Expected",
        "One Moment Changed Everything About {subject}",
        "{subject}: Watch What Happens Next",
        "The Part of {subject} I Had to Replay",
        "{subject} Came Out of Nowhere",
        "I Was Not Ready for {subject}",
    ),
}

_SPOKEN_TITLE_TEMPLATES: dict[str, tuple[str, ...]] = {
    "big_finish": (
        "“{subject}” — Wait for the Payoff",
        "What Happened After “{subject}”",
        "The Ending After “{subject}” Says It All",
        "“{subject}” Was Only the Beginning",
        "The Final Seconds After “{subject}”",
        "Why “{subject}” Was the Turning Point",
        "“{subject}” — Then Came the Best Part",
        "The Payoff Behind “{subject}”",
        "Everything Led Back to “{subject}”",
        "“{subject}” Set Up the Perfect Ending",
        "The Moment After “{subject}” Changed Everything",
        "“{subject}” — One Last Twist",
    ),
    "fast_start": (
        "“{subject}” — And We Were Off",
        "Everything Started With “{subject}”",
        "“{subject}” Set the Tone Instantly",
        "The Chaos Started Right After “{subject}”",
        "“{subject}” — Zero Warm-Up",
        "How “{subject}” Kicked Everything Off",
        "“{subject}” Changed the Energy Immediately",
        "The First Seconds After “{subject}”",
        "“{subject}” — Straight Into the Action",
        "It All Started With “{subject}”",
        "“{subject}” Hit From the First Second",
        "Why “{subject}” Was the Perfect Opener",
    ),
    "escalation": (
        "“{subject}” — Then Everything Escalated",
        "What Happened Right After “{subject}”",
        "How “{subject}” Changed the Whole Moment",
        "“{subject}” Was Just the Start",
        "The Moment After “{subject}” Got Wilder",
        "“{subject}” — And It Kept Building",
        "Why “{subject}” Became the Turning Point",
        "Everything Shifted After “{subject}”",
        "“{subject}” Took This Somewhere Unexpected",
        "The Build-Up After “{subject}” Was Unreal",
        "“{subject}” — Watch the Energy Change",
        "It Started With “{subject}” and Did Not Stop",
    ),
    "sustained": (
        "“{subject}” — Every Second Mattered",
        "Why “{subject}” Had Everyone Locked In",
        "The Intensity Behind “{subject}”",
        "“{subject}” Never Let the Energy Drop",
        "The Full Story Behind “{subject}”",
        "“{subject}” — No Breaks, Just Momentum",
        "What Made “{subject}” So Intense",
        "“{subject}” Kept the Pressure On",
        "The Moment Built Around “{subject}”",
        "“{subject}” — The Energy Never Stopped",
        "Why “{subject}” Hit Different",
        "Every Part of “{subject}” Counted",
    ),
    "surprise": (
        "“{subject}” — Then the Clip Took a Turn",
        "What Happened After “{subject}”",
        "The Unexpected Part of “{subject}”",
        "“{subject}” Changed the Whole Moment",
        "Why “{subject}” Caught Me Off Guard",
        "“{subject}” — I Had to Replay This",
        "The Twist Hidden Inside “{subject}”",
        "“{subject}” Was Not Going Where I Expected",
        "One Second Changed Everything After “{subject}”",
        "The Part After “{subject}” Nobody Expected",
        "“{subject}” — Watch What Happens Next",
        "I Was Not Ready for “{subject}”",
    ),
    "explainer": (
        "Why “{subject}” Matters More Than You Think",
        "The Bigger Story Behind “{subject}”",
        "What Most People Miss About “{subject}”",
        "“{subject}” Explained in One Clip",
        "The Key Detail Behind “{subject}”",
        "Why “{subject}” Changes the Bigger Picture",
        "The Truth Behind “{subject}”",
        "“{subject}” Changes How You See This",
        "One Fact About “{subject}” That Sticks",
        "Why “{subject}” Is So Important",
        "The Hidden Meaning of “{subject}”",
        "What “{subject}” Really Means",
    ),
}


def _bounded_viral_title(value: str, limit: int = 86) -> str:
    title = re.sub(r"\s+", " ", value).strip(" .—–-")
    if len(title) <= limit:
        return title
    shortened = title[: limit + 1].rsplit(" ", 1)[0].rstrip(" ,.;:—–-")
    return shortened or title[:limit].rstrip(" ,.;:—–-")


def generate_viral_title(
    clip_path: Path,
    source_title: str = "",
    caption_text: str = "",
    transcript_text: str = "",
    variation_seed: str = "",
    excluded_titles: set[str] | tuple[str, ...] | list[str] = (),
) -> dict:
    """Create a fresh, truthful hook from spoken words, creator context, and clip energy."""
    try:
        pattern, _ = _classify_clip_energy(_audio_rms_per_second(clip_path))
    except (OSError, subprocess.SubprocessError, RuntimeError):
        pattern = "surprise"

    caption_subject = _clean_title_subject(caption_text)
    transcript_subject = _transcript_title_subject(transcript_text)
    context_subject = _clean_title_subject(_clean_title_context(source_title), word_limit=8)
    subject = caption_subject or transcript_subject or context_subject
    semantic_source = "creator_caption" if caption_subject else "transcript" if transcript_subject else "source_context"
    if not caption_subject and transcript_subject and context_subject:
        first_transcript_word = transcript_subject.split()[0].casefold().strip(".,!?")
        if first_transcript_word in {"he", "her", "his", "it", "its", "she", "that", "their", "they", "this"}:
            subject = re.sub(r"^(?:How|What|Why)\s+", "", context_subject, flags=re.IGNORECASE).strip()
    transcript_terms = set(re.findall(r"[\w'’]+", transcript_text.casefold(), flags=re.UNICODE))
    explainer_cues = {
        "because", "causes", "ecosystem", "effects", "explains", "fact", "important", "keystone",
        "means", "reason", "role", "science", "species", "system", "works",
    }
    title_pattern = "explainer" if transcript_subject and len(transcript_terms & explainer_cues) >= 2 else pattern

    if subject:
        templates = _SPOKEN_TITLE_TEMPLATES[title_pattern] if semantic_source in {"creator_caption", "transcript"} else _TITLE_TEMPLATES[pattern]
        candidates = [_bounded_viral_title(template.format(subject=subject)) for template in templates]
        if context_subject and context_subject.casefold() != subject.casefold():
            if semantic_source == "source_context":
                candidates.extend([
                    _bounded_viral_title(f"{context_subject}: {subject}"),
                    _bounded_viral_title(f"The {context_subject} Moment Built Around {subject}"),
                ])
            else:
                candidates.extend([
                    _bounded_viral_title(f"{context_subject} — The Detail Most People Miss"),
                    _bounded_viral_title(f"Why {context_subject} Matters Here"),
                    _bounded_viral_title(f"{context_subject} Through One Powerful Moment"),
                ])
    else:
        candidates = [
            "The Moment Everything Changed",
            "This Escalated Faster Than Expected",
            "The Ending Is Worth the Wait",
            "One Second Changed the Whole Clip",
            "The Part I Had to Replay",
            "It Got Wilder With Every Second",
            "This Turned Into Something Else",
            "The Payoff Came Out of Nowhere",
        ]
        semantic_source = "energy"

    # A unique export id changes the order; recent-history exclusions guarantee
    # that repeated exports receive a different recommendation while good hooks
    # stay attached to truthful clip context.
    seed = variation_seed or f"{clip_path}:{source_title}:{caption_text}:{transcript_text}"
    candidates = list(dict.fromkeys(candidates))
    candidates.sort(key=lambda candidate: hashlib.sha256(f"{seed}\0{candidate}".encode("utf-8")).digest())
    excluded = {title.casefold() for title in excluded_titles}
    title = next((candidate for candidate in candidates if candidate.casefold() not in excluded), "")
    if not title:
        adjectives = (
            "Clutch", "Chaotic", "Electric", "Epic", "Fearless", "Iconic", "Intense", "Legendary",
            "Raw", "Relentless", "Unfiltered", "Unexpected", "Unreal", "Untamed", "Wild", "Zero-Chill",
        )
        moments = (
            "Breakdown", "Ending", "Energy", "Finish", "Moment", "Payoff", "Reaction", "Replay",
            "Sequence", "Showdown", "Surprise", "Turn", "Twist", "Vibe", "Win", "Wildcard",
        )
        fallback_variants = [
            _bounded_viral_title(f"{subject or 'The Clip'} — {adjective} {moment}")
            for adjective in adjectives
            for moment in moments
        ]
        fallback_variants.sort(
            key=lambda candidate: hashlib.sha256(f"{seed}\0fallback\0{candidate}".encode("utf-8")).digest()
        )
        title = next(candidate for candidate in fallback_variants if candidate.casefold() not in excluded)
    strategy = f"{semantic_source}_{title_pattern}"

    return {
        "title": title,
        "filename": safe_export_filename(title),
        "strategy": strategy,
    }


def _face_crop(
    info: VideoInfo,
    corner: FaceCorner,
    width_fraction: float,
    height_fraction: float,
    inset_x_fraction: float = 0.0,
    inset_y_fraction: float = 0.0,
) -> tuple[int, int, int, int]:
    fw = min(info.width, max(64, int(info.width * width_fraction)))
    fh = min(info.height, max(64, int(info.height * height_fraction)))
    inset_x = max(0, int(info.width * inset_x_fraction))
    inset_y = max(0, int(info.height * inset_y_fraction))
    max_x = max(0, info.width - fw)
    max_y = max(0, info.height - fh)
    if corner.endswith("right"):
        x = max(0, max_x - inset_x)
    else:
        x = min(max_x, inset_x)
    if corner.startswith("bottom"):
        y = max(0, max_y - inset_y)
    else:
        y = min(max_y, inset_y)
    return fw, fh, x, y


def _caption_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
        Path("/Library/Fonts/Arial Bold.ttf"),
        Path("/System/Library/Fonts/SFNS.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            try:
                return ImageFont.truetype(str(candidate), size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def _emoji_font(size: int = 64) -> ImageFont.FreeTypeFont | None:
    candidate = Path("/System/Library/Fonts/Apple Color Emoji.ttc")
    if not candidate.exists():
        return None
    # Apple Color Emoji is a bitmap-backed font and accepts only a handful of
    # exact pixel sizes. Asking Pillow for a normal caption size such as 86px
    # raises ``invalid pixel size`` and used to make emojis fall back to square
    # missing-glyph boxes. Pick the closest available strike instead.
    requested = max(16, int(size))
    for strike in sorted((20, 32, 40, 48, 64, 96, 160), key=lambda value: abs(value - requested)):
        try:
            return ImageFont.truetype(str(candidate), strike)
        except OSError:
            continue
    return None


def center_caption_overlay(
    image: Image.Image,
    vertical_position: CaptionPosition = "center",
) -> Image.Image:
    """Optically position visible caption pixels on a transparent canvas.

    Font advance widths are not reliable for mixed text and color emoji. Their
    side bearings vary between Apple Color Emoji, Segoe UI Emoji, and Linux
    fallbacks, which can make a mathematically positioned text run look shifted.
    Positioning the final alpha bounds keeps every renderer and platform aligned.
    """
    if vertical_position not in {"top", "center", "bottom"}:
        raise ValueError("Unsupported square-caption position.")
    source = image.convert("RGBA")
    bounds = source.getchannel("A").getbbox()
    if bounds is None:
        return source

    visible = source.crop(bounds)
    centered = Image.new("RGBA", source.size, (0, 0, 0, 0))
    x = round((source.width - visible.width) / 2)
    edge_margin = round(source.height * 0.089)
    if vertical_position == "top":
        y = min(edge_margin, max(0, source.height - visible.height))
    elif vertical_position == "bottom":
        y = max(0, source.height - visible.height - edge_margin)
    else:
        y = round((source.height - visible.height) / 2)
    centered.alpha_composite(visible, (x, y))
    return centered


def _center_caption_file(destination: Path, vertical_position: CaptionPosition) -> None:
    with Image.open(destination) as rendered:
        centered = center_caption_overlay(rendered, vertical_position)
    centered.save(destination, format="PNG")


def _render_square_caption_macos(text: str, destination: Path, font_scale: float) -> bool:
    """Render caption text through Core Text so full emoji sequences stay intact.

    Pillow can draw simple color emoji glyphs, but without a complex-text
    shaper it splits skin tones, flags, keycaps, and ZWJ family/profession emoji
    into separate characters. Core Text applies the same Apple Color Emoji
    shaping used by native Mac apps. A Core Graphics bitmap context keeps this
    path safe inside FastAPI's background export workers.
    """
    if sys.platform != "darwin":
        return False
    try:
        import CoreText
        import Quartz
        from Foundation import NSAttributedString, NSURL
    except (ImportError, AttributeError):
        return False

    clean = text.strip()[:160]
    if not clean:
        Image.new("RGBA", (1080, 1080), (0, 0, 0, 0)).save(destination)
        return True

    scale = min(1.75, max(0.50, float(font_scale)))
    # Core Text font sizes are typographic points; 106pt closely matches the
    # visible cap height of Pillow's existing 86px caption preset.
    font_size = round(106 * scale)

    white = Quartz.CGColorCreateGenericRGB(1, 1, 1, 1)
    black = Quartz.CGColorCreateGenericRGB(0, 0, 0, 1)

    def attributes(size: int) -> dict:
        return {
            CoreText.kCTFontAttributeName: CoreText.CTFontCreateWithName("Arial-BoldMT", size, None),
            CoreText.kCTForegroundColorAttributeName: white,
            CoreText.kCTStrokeColorAttributeName: black,
            # Negative values draw both the fill and an outside stroke. The
            # value is a percentage of the font size, not a point measurement.
            CoreText.kCTStrokeWidthAttributeName: -5.0,
        }

    def make_line(value: str, style: dict):
        attributed = NSAttributedString.alloc().initWithString_attributes_(value, style)
        return CoreText.CTLineCreateWithAttributedString(attributed)

    def line_width(value: str, style: dict) -> float:
        line = make_line(value, style)
        width, _, _, _ = CoreText.CTLineGetTypographicBounds(line, None, None, None)
        return float(width)

    def wrap_lines(value: str, style: dict) -> list[str]:
        lines: list[str] = []
        for source_line in value.replace("\r", "").split("\n"):
            words = source_line.split()
            if not words:
                lines.append("")
                continue
            current = words[0]
            for word in words[1:]:
                candidate = f"{current} {word}"
                if line_width(candidate, style) <= 960:
                    current = candidate
                else:
                    lines.append(current)
                    current = word
            lines.append(current)
        return lines

    # Core Text measures complete grapheme clusters correctly. Shrink only
    # when the caption would otherwise need more than three centered lines.
    while font_size > 32:
        style = attributes(font_size)
        lines = wrap_lines(clean, style)
        if len(lines) <= 3:
            break
        font_size -= 4
    else:
        style = attributes(font_size)
        lines = wrap_lines(clean, style)

    lines = lines[:3]
    line_height = font_size * 1.18
    block_height = min(960, max(line_height, line_height * len(lines)))
    color_space = Quartz.CGColorSpaceCreateDeviceRGB()
    context = Quartz.CGBitmapContextCreate(
        None,
        1080,
        1080,
        8,
        0,
        color_space,
        Quartz.kCGImageAlphaPremultipliedLast,
    )
    if context is None:
        return False
    Quartz.CGContextClearRect(context, Quartz.CGRectMake(0, 0, 1080, 1080))
    Quartz.CGContextSetTextMatrix(context, Quartz.CGAffineTransformIdentity)

    block_bottom = (1080 - block_height) / 2
    for index, value in enumerate(lines):
        line = make_line(value, style)
        width, ascent, descent, _ = CoreText.CTLineGetTypographicBounds(line, None, None, None)
        slot_bottom = block_bottom + (len(lines) - index - 1) * line_height
        baseline = slot_bottom + (line_height - ascent - descent) / 2 + descent
        Quartz.CGContextSetTextPosition(
            context,
            (1080 - float(width)) / 2,
            baseline,
        )
        CoreText.CTLineDraw(line, context)

    image = Quartz.CGBitmapContextCreateImage(context)
    destination_url = NSURL.fileURLWithPath_(str(destination))
    image_destination = Quartz.CGImageDestinationCreateWithURL(destination_url, "public.png", 1, None)
    if image_destination is None:
        return False
    Quartz.CGImageDestinationAddImage(image_destination, image, None)
    return bool(Quartz.CGImageDestinationFinalize(image_destination))


def _caption_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for character in text:
        if character in {"\ufe0f", "\ufe0e", "\u200d"} and tokens:
            tokens[-1] += character
        elif tokens and tokens[-1].endswith("\u200d"):
            tokens[-1] += character
        else:
            tokens.append(character)
    return tokens


def _is_emoji(token: str) -> bool:
    return any(character in {"❤", "♥"} or ord(character) >= 0x1F000 for character in token)


def _caption_length(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    emoji_font: ImageFont.FreeTypeFont | None,
) -> float:
    return sum(
        draw.textlength(token, font=emoji_font if emoji_font and _is_emoji(token) else font)
        for token in _caption_tokens(text)
    )


def _wrap_caption(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    emoji_font: ImageFont.FreeTypeFont | None,
    max_width: int,
) -> list[str]:
    lines: list[str] = []
    for paragraph in text.replace("\r", "").split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if _caption_length(draw, candidate, font, emoji_font) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines[:3]


def _render_square_caption(
    text: str,
    destination: Path,
    font_scale: float = 1.0,
    caption_position: CaptionPosition = "center",
) -> None:
    if _render_square_caption_macos(text, destination, font_scale):
        _center_caption_file(destination, caption_position)
        return

    canvas = Image.new("RGBA", (1080, 1080), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    clean = text.strip()[:160]
    scale = min(1.75, max(0.50, float(font_scale)))
    font_size = round(86 * scale)
    font = _caption_font(font_size)
    emoji_font = _emoji_font(font_size)
    lines = _wrap_caption(draw, clean, font, emoji_font, 960)
    while lines and max((_caption_length(draw, line, font, emoji_font) for line in lines), default=0) > 960 and font_size > 32:
        font_size -= 4
        font = _caption_font(font_size)
        emoji_font = _emoji_font(font_size)
        lines = _wrap_caption(draw, clean, font, emoji_font, 960)

    line_height = int(font_size * 1.18)
    block_height = line_height * len(lines)
    y = (1080 - block_height) / 2
    stroke = max(4, round(font_size * 0.065))
    for line in lines:
        tokens = _caption_tokens(line)
        widths = [
            draw.textlength(token, font=emoji_font if emoji_font and _is_emoji(token) else font)
            for token in tokens
        ]
        x = (1080 - sum(widths)) / 2
        for token, token_width in zip(tokens, widths):
            if emoji_font and _is_emoji(token):
                draw.text((x, y + round(font_size * 0.16)), token, font=emoji_font, embedded_color=True)
            else:
                fill = (244, 42, 54, 255) if _is_emoji(token) else (255, 255, 255, 255)
                draw.text(
                    (x, y),
                    token,
                    font=font,
                    fill=fill,
                    stroke_width=stroke,
                    stroke_fill=(5, 5, 5, 255),
                )
            x += token_width
        y += line_height
    center_caption_overlay(canvas, caption_position).save(destination)


def _render_sound_effect(effect: SoundEffect, destination: Path) -> None:
    """Copy one of the bundled creator-supplied sound effects."""
    asset_names = {
        "vine-boom": "vine-boom.wav",
        "check-sound": "check-sound.wav",
    }
    asset_name = asset_names.get(effect)
    if asset_name is None:
        raise ValueError("Unknown sound effect.")
    source = EFFECT_ASSETS_DIR / asset_name
    if not source.is_file():
        raise RuntimeError(f"The bundled {effect} sound asset is missing.")
    shutil.copyfile(source, destination)


def _render_visual_overlay(
    effect: VisualEffect,
    destination: Path,
    width: int,
    height: int,
    strength: float,
) -> None:
    strength = min(1.5, max(0.25, float(strength)))
    if effect == "white-flash":
        alpha = round(220 * min(1.0, strength))
        Image.new("RGBA", (width, height), (255, 255, 255, alpha)).save(destination)
        return

    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    source_x = round(width * 0.76)
    source_y = round(height * 0.23)
    smallest = min(width, height)

    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow, "RGBA")
    glow_radius = round(smallest * 0.30)
    glow_draw.ellipse(
        (source_x - glow_radius, source_y - glow_radius, source_x + glow_radius, source_y + glow_radius),
        fill=(255, 167, 76, round(92 * min(1.0, strength))),
    )
    from PIL import ImageFilter

    glow = glow.filter(ImageFilter.GaussianBlur(round(smallest * 0.12)))
    canvas.alpha_composite(glow)
    draw = ImageDraw.Draw(canvas, "RGBA")

    streak_alpha = round(100 * min(1.0, strength))
    streak_height = max(2, round(height * 0.006))
    draw.rectangle(
        (round(width * 0.10), source_y - streak_height, round(width * 0.96), source_y + streak_height),
        fill=(255, 190, 110, streak_alpha),
    )
    core_radius = max(10, round(smallest * 0.035))
    draw.ellipse(
        (source_x - core_radius, source_y - core_radius, source_x + core_radius, source_y + core_radius),
        fill=(255, 248, 210, round(245 * min(1.0, strength))),
    )

    center_x, center_y = width / 2, height / 2
    for position, radius, color in [
        (0.36, 0.055, (90, 180, 255, 78)),
        (0.56, 0.032, (255, 102, 170, 68)),
        (0.75, 0.082, (110, 255, 190, 48)),
        (1.08, 0.045, (255, 185, 86, 62)),
    ]:
        x = source_x + (center_x - source_x) * position
        y = source_y + (center_y - source_y) * position
        r = smallest * radius
        ring = tuple((*color[:3], round(color[3] * min(1.0, strength))))
        draw.ellipse((x - r, y - r, x + r, y + r), outline=ring, width=max(2, round(r * 0.12)))
    canvas.save(destination)


def _has_audio(path: Path) -> bool:
    result = subprocess.run(
        [
            ffmpeg_executable(), "-v", "error", "-i", str(path),
            "-map", "0:a:0", "-frames:a", "1", "-f", "null", "-",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


# Some stream recordings store AAC audio whose frames switch between one and two
# channels while the file declares a single layout for all of them (a Twitch VOD
# declared mono ran mono, stereo from 0:01.6, then mono again from 0:07.3).
# FFmpeg's decoder follows the first switch but not the way back: every later
# frame is decoded into a stale channel setup, and in the threaded command-line
# tool the result even differs from run to run. Speech in such a file came out
# garbled enough to cost Whisper whole sentences. The repair labels each frame
# with the layout its first element (one channel or a channel pair) says it has,
# decodes that, puts every frame back at its own timestamp, and keeps the result
# as a lossless FLAC that everything which listens to the audio reads instead.
_AUDIO_REPAIR_LOCK = threading.Lock()
_AUDIO_REPAIRS: dict[tuple[str, int, int], Path | None] = {}
_AUDIO_REPAIR_DIR = Path(tempfile.gettempdir()) / f"{APP_SLUG}-repaired-audio"
_AUDIO_REPAIR_KEEP = 4
_ADTS_SAMPLE_RATES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000, 7350)


def audio_source(source: Path) -> Path:
    """The file to decode a video's audio from: the video itself, or its repaired audio."""
    try:
        resolved = Path(source).resolve()
        stat = resolved.stat()
    except OSError:
        return source
    key = (str(resolved), int(stat.st_size), int(stat.st_mtime_ns))
    with _AUDIO_REPAIR_LOCK:
        if key not in _AUDIO_REPAIRS or (_AUDIO_REPAIRS[key] is not None and not _AUDIO_REPAIRS[key].is_file()):
            try:
                _AUDIO_REPAIRS[key] = _repair_switching_aac(resolved, key)
            except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
                _AUDIO_REPAIRS[key] = None
        repaired = _AUDIO_REPAIRS[key]
    return repaired if repaired is not None else source


def _adts_frames(stream):
    """Yield (header_size, frame bytes) for each ADTS frame in a byte stream."""
    buffer = b""
    while True:
        chunk = stream.read(1 << 20)
        buffer += chunk
        position = 0
        while len(buffer) - position >= 9:
            if buffer[position] != 0xFF or buffer[position + 1] & 0xF0 != 0xF0:
                raise ValueError("Lost ADTS sync.")
            length = ((buffer[position + 3] & 0x03) << 11) | (buffer[position + 4] << 3) | (buffer[position + 5] >> 5)
            if length < 9 or len(buffer) - position < length:
                break
            yield (7 if buffer[position + 1] & 1 else 9), buffer[position:position + length]
            position += length
        buffer = buffer[position:]
        if not chunk:
            return


def _repair_switching_aac(source: Path, key: tuple[str, int, int]) -> Path | None:
    """A FLAC of the source's audio when its AAC frames switch layout, else None."""
    ffmpeg = ffmpeg_executable()
    _AUDIO_REPAIR_DIR.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(repr(key).encode()).hexdigest()[:24]
    repaired = _AUDIO_REPAIR_DIR / f"{name}.flac"
    if repaired.is_file():
        return repaired
    relabelled = _AUDIO_REPAIR_DIR / f"{name}.aac"
    copy = subprocess.Popen(
        [ffmpeg, "-v", "error", "-i", str(source), "-map", "0:a:0", "-c:a", "copy", "-f", "adts", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    switches = 0
    sample_rate = 0
    try:
        with relabelled.open("wb") as output:
            for header_size, frame in _adts_frames(copy.stdout):
                declared = ((frame[2] & 0x01) << 2) | (frame[3] >> 6)
                sample_rate = _ADTS_SAMPLE_RATES[min(12, (frame[2] >> 2) & 0x0F)]
                if declared not in {1, 2}:
                    return None  # Surround layouts are left alone.
                actual = {0: 1, 1: 2}.get(frame[header_size] >> 5)  # One channel, or a channel pair.
                if actual is not None and actual != declared:
                    switches += 1
                    frame = bytearray(frame)
                    frame[2] = (frame[2] & 0xFE) | (actual >> 2)
                    frame[3] = (frame[3] & 0x3F) | ((actual & 0x03) << 6)
                output.write(frame)
        copy.wait()
        if copy.returncode != 0 or not switches:
            return None
        timestamps = _packet_timestamps(ffmpeg, source, sample_rate)
        _decode_onto_timeline(ffmpeg, relabelled, repaired, sample_rate, timestamps)
    finally:
        if copy.poll() is None:
            copy.kill()
        copy.stdout.close()
        copy.wait()
        relabelled.unlink(missing_ok=True)
    kept = sorted(_AUDIO_REPAIR_DIR.glob("*.flac"), key=lambda path: path.stat().st_mtime, reverse=True)
    for stale in kept[_AUDIO_REPAIR_KEEP:]:
        stale.unlink(missing_ok=True)
    return repaired


def _packet_timestamps(ffmpeg: str, source: Path, sample_rate: int) -> list[tuple[int, int]]:
    """(start, length) of every audio packet in samples, from FFmpeg's framecrc listing."""
    listing = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(source), "-map", "0:a:0", "-c:a", "copy", "-f", "framecrc", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, text=True,
    ).stdout
    # FFmpeg counts a video's time from its earliest timestamp, which is not always
    # zero (MPEG-TS recordings often start at 1.4 s); the FLAC starts there too.
    banner = subprocess.run([ffmpeg, "-hide_banner", "-i", str(source)], capture_output=True, text=True).stderr
    match = re.search(r"start: (-?[\d.]+)", banner)
    origin = round(float(match.group(1)) * sample_rate) if match else 0
    time_base = Fraction(1, sample_rate)
    packets: list[tuple[int, int]] = []
    for line in listing.splitlines():
        if line.startswith("#tb 0:"):
            time_base = Fraction(line.split(":", 1)[1].strip())
        elif line and not line.startswith("#"):
            fields = [field.strip() for field in line.split(",")]
            start = round(int(fields[2]) * time_base * sample_rate) - origin
            length = round(int(fields[3]) * time_base * sample_rate)
            packets.append((start, length))
    return packets


def _decode_onto_timeline(
    ffmpeg: str,
    relabelled: Path,
    output: Path,
    sample_rate: int,
    packets: list[tuple[int, int]],
) -> None:
    """Decode relabelled ADTS and place each frame at its packet's timestamp.

    Raw ADTS carries no timestamps, so silence fills any gap in the original and
    overlaps are dropped; the FLAC starts at the video's zero like its audio did.
    """
    frame_bytes = 2 * _EDIT_CHANNELS
    decoder = subprocess.Popen(
        [
            ffmpeg, "-v", "error", "-f", "aac", "-i", str(relabelled),
            "-ac", str(_EDIT_CHANNELS), "-ar", str(sample_rate), "-f", "s16le", "pipe:1",
        ],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    encoder = subprocess.Popen(
        [
            ffmpeg, "-y", "-v", "error", "-f", "s16le", "-ar", str(sample_rate), "-ac", str(_EDIT_CHANNELS),
            "-i", "pipe:0", *_LOSSLESS_AUDIO, str(output),
        ],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        written = 0  # Samples on the output timeline so far.
        for start, length in packets:
            data = decoder.stdout.read(length * frame_bytes) if length > 0 else b""
            if len(data) < length * frame_bytes:
                break
            if start > written:
                gap = start - written
                encoder.stdin.write(b"\0" * (gap * frame_bytes))
                written += gap
            skip = min(length, written - start)
            if skip < length:
                encoder.stdin.write(data[skip * frame_bytes:])
                written += length - skip
        encoder.stdin.close()
        if encoder.wait() != 0:
            raise RuntimeError("Could not store the repaired audio.")
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    finally:
        if encoder.poll() is None:
            encoder.kill()
            encoder.wait()
        decoder.kill()
        decoder.stdout.close()
        decoder.wait()


def _video_filter_chain(video_filter: VideoFilter) -> str:
    try:
        return VIDEO_FILTER_CHAINS[video_filter]
    except KeyError as exc:
        raise ValueError("Unknown video filter.") from exc


def _analysis_cache_key(source: Path, start: float, end: float, *settings: object) -> tuple[object, ...]:
    resolved = Path(source).resolve()
    stat = resolved.stat()
    return (
        str(resolved), int(stat.st_size), int(stat.st_mtime_ns),
        round(float(start) * 1_000), round(float(end) * 1_000), *settings,
    )


def _store_bounded_cache(cache: dict, key: tuple[object, ...], value: object) -> None:
    cache[key] = value
    while len(cache) > _FULL_LENGTH_ANALYSIS_CACHE_LIMIT:
        cache.pop(next(iter(cache)))


def _transcribe_words_cached(
    source: Path,
    start: float,
    end: float,
    include_fillers: bool = False,
) -> list[CaptionWord]:
    """Reuse one offline transcript across cleanup, captions, sounds, and titles."""
    resolved = Path(source).resolve()
    cache_key = _analysis_cache_key(
        resolved, max(0.0, start), max(start + 0.1, end), "transcript", bool(include_fillers)
    )
    with _TRANSCRIPT_CACHE_LOCK:
        cached = _TRANSCRIPT_CACHE.get(cache_key)
        if cached is not None:
            return list(cached)

    if include_fillers:
        words = transcribe_words(audio_source(resolved), start, end, ffmpeg_executable(), include_fillers=True)
    else:
        words = transcribe_words(audio_source(resolved), start, end, ffmpeg_executable())
    with _TRANSCRIPT_CACHE_LOCK:
        _TRANSCRIPT_CACHE[cache_key] = tuple(words)
        while len(_TRANSCRIPT_CACHE) > _TRANSCRIPT_CACHE_LIMIT:
            _TRANSCRIPT_CACHE.pop(next(iter(_TRANSCRIPT_CACHE)))
    return list(words)


def _clip_audio_envelope(
    source: Path,
    start: float,
    end: float,
    sample_rate: int = 8_000,
    hop_seconds: float = 0.10,
) -> tuple[np.ndarray, float]:
    """Decode a clip into a fine-grained, memory-bounded loudness envelope."""
    duration = max(0.1, float(end) - float(start))
    cache_key = _analysis_cache_key(
        source, max(0.0, start), max(start + 0.1, end), "envelope", sample_rate, round(hop_seconds, 4)
    )
    with _FULL_LENGTH_ANALYSIS_CACHE_LOCK:
        cached = _CLIP_ENVELOPE_CACHE.get(cache_key)
        if cached is not None:
            return cached
    command = [
        ffmpeg_executable(), "-v", "error",
        "-ss", f"{max(0.0, float(start)):.3f}", "-i", str(audio_source(source)),
        "-t", f"{duration:.3f}", "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-f", "s16le", "pipe:1",
    ]
    frame_size = max(1, round(sample_rate * hop_seconds))
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if process.stdout is None:
        raise RuntimeError("FFmpeg did not provide decoded audio.")
    envelope_values: list[float] = []
    bytes_per_frame = frame_size * np.dtype(np.int16).itemsize
    try:
        while True:
            raw = _read_exactly(process.stdout, bytes_per_frame)
            if not raw:
                break
            usable = len(raw) - (len(raw) % 2)
            samples = np.frombuffer(raw[:usable], dtype=np.int16).astype(np.float32) / 32768.0
            if samples.size < frame_size:
                samples = np.pad(samples, (0, frame_size - samples.size))
            envelope_values.append(float(np.sqrt(np.mean(np.square(samples)))))
        return_code = process.wait()
        if return_code != 0:
            raise RuntimeError("FFmpeg could not analyze the selected audio.")
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill()
            process.wait()
    if not envelope_values:
        result = (np.zeros(max(1, math.ceil(duration / hop_seconds)), dtype=np.float32), hop_seconds)
    else:
        result = (np.asarray(envelope_values, dtype=np.float32), frame_size / sample_rate)
    with _FULL_LENGTH_ANALYSIS_CACHE_LOCK:
        _store_bounded_cache(_CLIP_ENVELOPE_CACHE, cache_key, result)
    return result


def _clip_scene_change_times(source: Path, start: float, end: float) -> list[float]:
    """Return hard-cut timestamps relative to a short selected clip."""
    duration = max(0.1, float(end) - float(start))
    cache_key = _analysis_cache_key(source, max(0.0, start), max(start + 0.1, end), "scenes")
    with _FULL_LENGTH_ANALYSIS_CACHE_LOCK:
        cached = _SCENE_CHANGE_CACHE.get(cache_key)
        if cached is not None:
            return list(cached)
    result = subprocess.run(
        [
            ffmpeg_executable(), "-v", "info",
            "-skip_frame", "nokey",
            "-ss", f"{max(0.0, float(start)):.3f}", "-i", str(source),
            "-t", f"{duration:.3f}", "-an",
            "-vf", "scale=160:-2,select=gt(scene\\,0.30),showinfo",
            "-fps_mode", "vfr", "-f", "null", "-",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        return []
    output = result.stderr.decode("utf-8", errors="replace")
    scenes = [
        value
        for value in (float(match) for match in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", output))
        if 0.35 <= value <= duration - 0.20
    ]
    with _FULL_LENGTH_ANALYSIS_CACHE_LOCK:
        _store_bounded_cache(_SCENE_CHANGE_CACHE, cache_key, tuple(scenes))
    return scenes


def _smart_sound_times_from_signals(
    envelope: np.ndarray,
    hop_seconds: float,
    duration: float,
    sound_effect: SoundEffect,
    scene_times: list[float] | tuple[float, ...] = (),
    fallback_time: float = 1.0,
    semantic_cues: list[tuple[float, str]] | tuple[tuple[float, str], ...] = (),
    blocked_times: list[float] | tuple[float, ...] = (),
) -> list[float]:
    """Rank likely punchline endings, reactions, and cuts for one selected sound.

    This deliberately relies on local audio/visual structure rather than claiming
    to understand the words being spoken. Spacing and quality gates prevent
    repetitive over-editing.
    """
    clip_duration = max(0.1, float(duration))
    if sound_effect == "none":
        return []

    # Leave enough tail after a smart trigger for the audible part of the
    # effect to land. Previously a fallback could be scheduled only 0.20s
    # before the end of the clip, which made effects with a short lead-in
    # effectively silent after the finished MP4 was trimmed.
    target_tail = 0.90 if sound_effect == "vine-boom" else 0.65
    tail_room = min(target_tail, max(0.10, clip_duration * 0.25))
    latest_trigger = max(0.0, clip_duration - tail_room)

    values = np.asarray(envelope, dtype=np.float32)
    values = np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0)
    candidates: list[tuple[float, float, str]] = []
    if values.size >= 5 and float(np.max(values, initial=0.0)) >= 1e-6:
        floor = float(np.percentile(values, 18))
        ceiling = float(np.percentile(values, 94))
        normalized = np.clip((values - floor) / max(ceiling - floor, 1e-6), 0, 1)
        smoothed = np.convolve(normalized, np.ones(3, dtype=np.float32) / 3, mode="same")

        for index in range(4, max(4, len(smoothed) - 4)):
            before = smoothed[max(0, index - 6):index]
            after = smoothed[index:min(len(smoothed), index + 4)]
            pre_level = float(np.mean(before)) if before.size else 0.0
            post_level = float(np.mean(after)) if after.size else 0.0
            pre_peak = float(np.max(before, initial=0.0))
            post_peak = float(np.max(after, initial=0.0))
            drop = pre_level - post_level
            rise = post_peak - float(np.mean(smoothed[max(0, index - 4):index]))

            if pre_level >= 0.24 and drop >= 0.14 and post_level <= 0.46:
                score = 0.60 * pre_level + 1.12 * drop + 0.20 * pre_peak
                candidates.append((index * hop_seconds + 0.06, score, "phrase-end"))
            if post_peak >= 0.54 and rise >= 0.18:
                score = 0.68 * post_peak + 0.95 * rise
                candidates.append((index * hop_seconds, score, "reaction"))
            if smoothed[index] >= 0.72 and smoothed[index] >= max(smoothed[index - 2:index + 3]):
                candidates.append((index * hop_seconds, 0.62 * float(smoothed[index]), "peak"))

    for scene_time in scene_times:
        candidates.append((float(scene_time) + 0.03, 0.78, "scene"))

    for cue_time, cue_kind in semantic_cues:
        if cue_kind in {"vine-cue", "check-cue"}:
            candidates.append((float(cue_time), 1.0, cue_kind))

    if sound_effect == "check-sound":
        weights = {
            "phrase-end": 0.34,
            "reaction": 1.28,
            "peak": 0.96,
            "scene": 0.58,
            "vine-cue": 0.08,
            "check-cue": 2.70,
        }
        spacing = 2.6
    else:
        weights = {
            "phrase-end": 1.30,
            "reaction": 0.72,
            "peak": 0.40,
            "scene": 0.24,
            "vine-cue": 2.70,
            "check-cue": 0.08,
        }
        spacing = 3.2
    max_hits = (
        min(24, max(6, int(clip_duration // 120) + 1))
        if clip_duration > 90
        else min(6, max(1, int(clip_duration // 9) + 1))
    )
    ranked = sorted(
        (
            (score * weights[kind], min(max(0.10, time_value), latest_trigger))
            for time_value, score, kind in candidates
            if 0.10 <= time_value <= latest_trigger
        ),
        reverse=True,
    )
    selected: list[float] = []
    best_score = ranked[0][0] if ranked else 0.0
    quality_floor = max(0.34, best_score * 0.48)
    for score, time_value in ranked:
        if (
            score < quality_floor
            or any(abs(time_value - existing) < spacing for existing in selected)
            or any(abs(time_value - blocked) < 1.15 for blocked in blocked_times)
        ):
            continue
        selected.append(time_value)
        if len(selected) >= max_hits:
            break

    if not selected:
        fallback_choices = [
            min(max(0.0, float(fallback_time)), latest_trigger),
            latest_trigger * 0.28,
            latest_trigger * 0.56,
            latest_trigger * 0.84,
        ]
        fallback = max(
            fallback_choices,
            key=lambda value: min((abs(value - blocked) for blocked in blocked_times), default=clip_duration),
        )
        selected = [fallback]
    return [round(value, 2) for value in sorted(selected)]


CHECK_SOUND_CUES = {
    "yes", "yeah", "nice", "perfect", "done", "win", "won", "winner", "clutch",
    "finally", "success", "correct", "great", "good", "easy", "complete", "completed",
    "nailed it", "got it", "lets go", "let us go", "there it is", "we did it",
}
VINE_BOOM_CUES = {
    "what", "why", "bro", "bruh", "ayo", "huh", "weird", "sus", "wait", "pause",
    "excuse me", "no way", "what the", "who said", "did he", "did she", "you said what",
}


def _semantic_sound_cues(words: list[CaptionWord] | tuple[CaptionWord, ...]) -> list[tuple[float, str]]:
    """Map local transcript words to positive-payoff and awkward-emphasis cues."""
    normalized = [re.sub(r"[^a-z0-9']+", " ", word.text.lower()).strip() for word in words]
    cues: list[tuple[float, str]] = []
    last_by_kind = {"check-cue": -10.0, "vine-cue": -10.0}
    for index, word in enumerate(words):
        cue_kind = None
        cue_length = 1
        for length in (3, 2, 1):
            if index + length > len(normalized):
                continue
            phrase = " ".join(normalized[index:index + length]).strip()
            if phrase in CHECK_SOUND_CUES:
                cue_kind, cue_length = "check-cue", length
                break
            if phrase in VINE_BOOM_CUES:
                cue_kind, cue_length = "vine-cue", length
                break
        if cue_kind is None:
            continue
        cue_time = round(min(words[index + cue_length - 1].end + 0.08, words[-1].end + 0.08), 3)
        if cue_time - last_by_kind[cue_kind] < 0.8:
            continue
        cues.append((cue_time, cue_kind))
        last_by_kind[cue_kind] = cue_time
    return cues


def suggest_sound_effect_placements(
    source: Path,
    start: float,
    end: float,
    sound_effects: list[SelectedSoundEffect] | tuple[SelectedSoundEffect, ...],
    transcript_words: list[CaptionWord] | tuple[CaptionWord, ...] = (),
    fallback_time: float = 1.0,
    timeline_cuts: list[tuple[float, float]] | tuple[tuple[float, float], ...] = (),
) -> dict[SelectedSoundEffect, list[float]]:
    """Analyze a clip once and assign each selected sound to different fitting moments."""
    selected = list(dict.fromkeys(effect for effect in sound_effects if effect in {"vine-boom", "check-sound"}))
    if not selected:
        return {}
    original_duration = max(0.1, float(end) - float(start))
    duration = original_duration
    try:
        envelope, hop_seconds = _clip_audio_envelope(source, start, end)
    except (OSError, subprocess.SubprocessError, RuntimeError):
        envelope, hop_seconds = np.zeros(1, dtype=np.float32), 0.10
    try:
        scenes = _clip_scene_change_times(source, start, end)
    except (OSError, subprocess.SubprocessError, RuntimeError):
        scenes = []
    merged_cuts = _merge_cut_intervals(list(timeline_cuts), original_duration)
    if merged_cuts:
        envelope = _cut_audio_envelope(envelope, hop_seconds, merged_cuts, original_duration)
        scenes = [
            _collapsed_timeline_time(scene, merged_cuts)
            for scene in scenes
            if not _time_is_removed(scene, merged_cuts)
        ]
        transcript_words = _remap_transcript_after_cuts(
            list(transcript_words), merged_cuts, original_duration
        )
        fallback_time = _collapsed_timeline_time(fallback_time, merged_cuts)
        duration = max(0.1, original_duration - sum(end - start for start, end in merged_cuts))
    semantic_cues = _semantic_sound_cues(transcript_words)
    cue_kind_for = {"vine-boom": "vine-cue", "check-sound": "check-cue"}
    selected.sort(
        key=lambda effect: sum(kind == cue_kind_for[effect] for _, kind in semantic_cues),
        reverse=True,
    )
    placements: dict[SelectedSoundEffect, list[float]] = {}
    occupied: list[float] = []
    for effect in selected:
        times = _smart_sound_times_from_signals(
            envelope,
            hop_seconds,
            duration,
            effect,
            scenes,
            fallback_time,
            semantic_cues,
            occupied,
        )
        placements[effect] = times
        occupied.extend(times)
    return placements


def suggest_sound_effect_times(
    source: Path,
    start: float,
    end: float,
    sound_effect: SoundEffect,
    fallback_time: float = 1.0,
) -> list[float]:
    """Analyze a selected clip and return smart, repeat-capable SFX timestamps."""
    if sound_effect == "none":
        return []
    return suggest_sound_effect_placements(
        source,
        start,
        end,
        [sound_effect],
        fallback_time=fallback_time,
    ).get(sound_effect, [])


def _apply_effects(
    source: Path,
    output: Path,
    duration: float,
    width: int,
    height: int,
    sound_effect: SoundEffect,
    visual_effect: VisualEffect,
    effect_time: float,
    sound_effect_times: list[float] | None,
    sound_volume: float,
    visual_strength: float,
    live_caption_ass: Path | None = None,
    sound_effect_placements: dict[SelectedSoundEffect, list[float]] | None = None,
    frame_rate: Fraction | None = None,
) -> None:
    trigger = min(max(0.0, float(effect_time)), max(0.0, duration - 0.05))
    volume = min(2.0, max(0.0, float(sound_volume)))
    strength = min(1.5, max(0.25, float(visual_strength)))
    temporary_paths: list[Path] = []
    inputs = [ffmpeg_executable(require_ass=live_caption_ass is not None), "-y", "-v", "error", "-i", str(source)]
    sound_indexes: dict[SelectedSoundEffect, int] = {}
    overlay_index: int | None = None
    placement_map: dict[SelectedSoundEffect, list[float]] = {}
    if sound_effect_placements is not None:
        for effect, supplied in sound_effect_placements.items():
            if effect not in {"vine-boom", "check-sound"}:
                raise ValueError("Unknown sound effect.")
            placement_map[effect] = sorted({
                round(min(max(0.0, float(value)), max(0.0, duration - 0.05)), 3)
                for value in supplied
            })
    elif sound_effect != "none":
        supplied = [trigger] if sound_effect_times is None else sound_effect_times
        placement_map[sound_effect] = sorted({
            round(min(max(0.0, float(value)), max(0.0, duration - 0.05)), 3)
            for value in supplied
        })

    try:
        for effect, sound_triggers in placement_map.items():
            if not sound_triggers:
                continue
            sound_file = tempfile.NamedTemporaryFile(prefix=f"{APP_SLUG}-sfx-", suffix=".wav", delete=False)
            sound_path = Path(sound_file.name)
            sound_file.close()
            temporary_paths.append(sound_path)
            _render_sound_effect(effect, sound_path)
            sound_indexes[effect] = 1 + len(sound_indexes)
            inputs += ["-i", str(sound_path)]

        if visual_effect in {"lens-flare", "white-flash"}:
            overlay_file = tempfile.NamedTemporaryFile(prefix=f"{APP_SLUG}-vfx-", suffix=".png", delete=False)
            overlay_path = Path(overlay_file.name)
            overlay_file.close()
            temporary_paths.append(overlay_path)
            _render_visual_overlay(visual_effect, overlay_path, width, height, strength)
            overlay_index = 1 + len(sound_indexes)
            inputs += ["-loop", "1", "-i", str(overlay_path)]

        filters: list[str] = []
        video_label = "0:v"
        if visual_effect == "punch-zoom":
            zoom_duration = min(0.70, max(0.25, duration - trigger))
            amount = 0.10 * strength
            end_time = trigger + zoom_duration
            scale = (
                f"if(between(t\\,{trigger:.3f}\\,{end_time:.3f})\\,"
                f"1+{amount:.4f}*sin(PI*(t-{trigger:.3f})/{zoom_duration:.3f})\\,1)"
            )
            filters.append(
                f"[0:v]scale=w='iw*{scale}':h='ih*{scale}':eval=frame,"
                f"crop={width}:{height}:(in_w-out_w)/2:(in_h-out_h)/2[vfx]"
            )
            video_label = "vfx"
        elif overlay_index is not None:
            if visual_effect == "lens-flare":
                fade_out_start, fade_out_duration = trigger + 0.18, 0.58
            else:
                fade_out_start, fade_out_duration = trigger + 0.07, 0.24
            filters.append(
                f"[{overlay_index}:v]format=rgba,"
                f"fade=t=in:st={trigger:.3f}:d=0.08:alpha=1,"
                f"fade=t=out:st={fade_out_start:.3f}:d={fade_out_duration:.3f}:alpha=1[overlayfx]"
            )
            filters.append(f"[0:v][overlayfx]overlay=0:0:eof_action=pass[vfx]")
            video_label = "vfx"

        if live_caption_ass is not None:
            caption_path = str(live_caption_ass).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
            filters.append(f"[{video_label}]ass=filename='{caption_path}'[captioned]")
            video_label = "captioned"

        audio_label: str | None = None
        if sound_indexes:
            effect_labels: list[str] = []
            duck_events: list[tuple[SelectedSoundEffect, float]] = []
            for effect_number, (effect, sound_index) in enumerate(sound_indexes.items()):
                sound_triggers = placement_map[effect]
                source_labels = [f"sfxsource{effect_number}_{index}" for index in range(len(sound_triggers))]
                if len(source_labels) > 1:
                    filters.append(
                        f"[{sound_index}:a]asplit={len(source_labels)}"
                        + "".join(f"[{label}]" for label in source_labels)
                    )
                else:
                    source_labels = [f"{sound_index}:a"]
                for index, (source_label, sound_trigger) in enumerate(zip(source_labels, sound_triggers)):
                    delay_ms = round(sound_trigger * 1000)
                    effect_label = f"sfx{effect_number}_{index}"
                    filters.append(
                        f"[{source_label}]adelay={delay_ms}:all=1,volume={volume:.3f}[{effect_label}]"
                    )
                    effect_labels.append(effect_label)
                    duck_events.append((effect, sound_trigger))
            effect_inputs = "".join(f"[{label}]" for label in effect_labels)
            if _has_audio(source):
                # AAC inputs can end a fraction of a second before the video on
                # some FFmpeg builds. Pad the source before mixing so a sound
                # placed near the end never truncates the finished audio track.
                filters.append(
                    f"[0:a]apad=whole_dur={duration:.3f},atrim=0:{duration:.3f}[paddedbase]"
                )
                duck_gain = max(0.30, 1.0 - 0.70 * min(1.0, volume))
                base_audio = "paddedbase"
                if duck_gain < 0.999:
                    duck_expressions: list[str] = []
                    for effect, sound_trigger in duck_events:
                        hold_seconds = 0.68 if effect == "check-sound" else 0.82
                        release_seconds = 0.96 if effect == "check-sound" else 1.10
                        attack_start = max(0.0, sound_trigger - 0.08)
                        attack_end = min(duration, max(sound_trigger, attack_start + 0.01))
                        release_start = min(duration, max(attack_end, sound_trigger + hold_seconds))
                        release_end = min(duration, max(release_start + 0.01, sound_trigger + release_seconds))
                        attack_duration = max(0.01, attack_end - attack_start)
                        release_duration = max(0.01, release_end - release_start)
                        duck_expressions.append(
                            f"if(between(t\\,{attack_start:.3f}\\,{attack_end:.3f})\\,"
                            f"1-(1-{duck_gain:.3f})*(t-{attack_start:.3f})/{attack_duration:.3f}\\,"
                            f"if(between(t\\,{attack_end:.3f}\\,{release_start:.3f})\\,{duck_gain:.3f}\\,"
                            f"if(between(t\\,{release_start:.3f}\\,{release_end:.3f})\\,"
                            f"{duck_gain:.3f}+(1-{duck_gain:.3f})*(t-{release_start:.3f})/{release_duration:.3f}\\,1)))"
                        )
                    filters.append(
                        f"[{base_audio}]volume='{'*'.join(f'({expression})' for expression in duck_expressions)}':"
                        "eval=frame[ducked]"
                    )
                    base_audio = "ducked"
                filters.append(
                    f"[{base_audio}]{effect_inputs}amix=inputs={len(effect_labels) + 1}:"
                    "duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95[aout]"
                )
            else:
                if len(effect_labels) > 1:
                    filters.append(
                        f"{effect_inputs}amix=inputs={len(effect_labels)}:duration=longest:"
                        "dropout_transition=0:normalize=0[effectmix]"
                    )
                    effect_source = "effectmix"
                else:
                    effect_source = effect_labels[0]
                filters.append(
                    f"[{effect_source}]apad=whole_dur={duration:.3f},"
                    f"atrim=0:{duration:.3f},alimiter=limit=0.95[aout]"
                )
            audio_label = "aout"

        def command_for(codec: list[str]) -> list[str]:
            command = list(inputs)
            if filters:
                command += ["-filter_complex", ";".join(filters)]
            command += ["-map", f"[{video_label}]" if video_label != "0:v" else "0:v:0"]
            if audio_label:
                command += ["-map", f"[{audio_label}]"]
            else:
                command += ["-map", "0:a?"]
            return command + [
                "-t", f"{duration:.3f}", *codec,
                "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart", str(output),
            ]

        if video_label == "0:v":
            # Only the sound changes, and the picture was encoded moments ago
            # with these settings, so it is copied rather than encoded again.
            _run(command_for(["-c:v", "copy"]))
        else:
            _encode_video(
                lambda codec: command_for([*codec, *_frame_rate_args(frame_rate)]),
                width, height, frame_rate, crf=20,
            )
    finally:
        for temporary in temporary_paths:
            temporary.unlink(missing_ok=True)


FILLER_WORDS = {
    "ah", "eh", "er", "erm", "hmm", "hm", "mhm", "uh", "uhh", "um", "umm",
    "basically", "literally",
}
FILLER_PHRASES = {
    ("i", "mean"),
    ("kind", "of"),
    ("sort", "of"),
    ("you", "know"),
}


def _normalized_caption_word(word: CaptionWord) -> str:
    return re.sub(r"[^a-z0-9']+", "", word.text.lower())


def _merge_cut_intervals(
    intervals: list[tuple[float, float]] | tuple[tuple[float, float], ...],
    duration: float,
) -> list[tuple[float, float]]:
    bounded = sorted(
        (max(0.0, float(start)), min(float(duration), float(end)))
        for start, end in intervals
        if float(end) - float(start) >= 0.025
    )
    merged: list[list[float]] = []
    for start, end in bounded:
        if end <= start:
            continue
        if merged and start <= merged[-1][1] + 0.035:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(round(start, 3), round(end, 3)) for start, end in merged]


def _filler_extent(
    levels: np.ndarray,
    onset: float,
    next_onset: float,
    depth: float = 0.65,
) -> tuple[float | None, float | None]:
    """Where a filler's own sound starts and stops, found in the voice-band level.

    The filler's loudness is measured just after its transcribed start and the gap
    level is the quietest nearby. The filler ends at the first dip that falls
    `depth` of the way from its loudness down to that gap level (the gap before the
    next word, or the start of a pause) and begins after the last such dip before
    it. Brief dips inside a drawn-out "uhhh" are shallower, so they do not end it.
    The next word's time is only an outer limit, because Whisper can place it late.
    """
    if levels.size == 0:
        return None, None
    frame = _speech_frame_seconds()
    centre = _SPEECH_WINDOW_SAMPLES / (2 * _SPEECH_SAMPLE_RATE)

    def index_at(seconds: float) -> int:
        return int(min(levels.size - 1, max(0, round((seconds - centre) / frame))))

    first = index_at(onset - 0.20)
    last = index_at(min(next_onset + 0.02, onset + 1.6))
    if last - first < 6:
        return None, None
    local = np.convolve(levels[first:last + 1], np.ones(3) / 3, mode="same")

    def at(seconds: float) -> int:
        return index_at(seconds) - first

    body = local[max(0, at(onset)):max(1, at(onset + 0.15))]
    if body.size == 0:
        return None, None
    loudness = float(np.percentile(body, 80))
    gap_level = float(np.percentile(local, 5))
    if loudness - gap_level < 9.0:
        return None, None  # no clear gap around this filler
    quiet = loudness - depth * (loudness - gap_level)

    end_index = None
    position = max(0, at(onset + 0.10))
    while position < local.size:
        if local[position] < quiet:
            while position + 1 < local.size and local[position + 1] <= local[position]:
                position += 1
            end_index = position
            break
        position += 1

    start_index = None
    lowest = max(0, at(onset - 0.15))
    position = min(local.size - 1, at(onset + 0.05))
    while position >= lowest:
        if local[position] < quiet:
            while position - 1 >= lowest and local[position - 1] <= local[position]:
                position -= 1
            start_index = position
            break
        position -= 1

    def time_of(index: int | None) -> float | None:
        return None if index is None else (first + index) * frame + centre

    return time_of(start_index), time_of(end_index)


def _filler_word_cut_intervals(
    words: list[CaptionWord] | tuple[CaptionWord, ...],
    duration: float,
    levels: np.ndarray | None = None,
    speech_segments: list[tuple[float, float]] | None = None,
) -> tuple[list[tuple[float, float]], int]:
    """Return filler-word cuts from the transcript, placed on the audio when available.

    With the voice-band levels, a filler is cut from the dip before its sound to
    the dip after it, so a drawn-out "uhhh" goes entirely. Where no clear dip is
    found the cut stays slightly inside the filler, so an imprecise word time leaves
    a sliver of "uh" rather than clipping the neighbouring word.
    """
    use_audio = levels is not None and levels.size > 0
    normalized = [_normalized_caption_word(word) for word in words]
    intervals: list[tuple[float, float]] = []
    removed_words = 0
    index = 0
    while index < len(words):
        phrase_length = 0
        if index + 1 < len(words) and tuple(normalized[index:index + 2]) in FILLER_PHRASES:
            phrase_length = 2
        elif normalized[index] in FILLER_WORDS:
            phrase_length = 1
        elif normalized[index] == "like":
            before_gap = words[index].start - words[index - 1].end if index else words[index].start
            after_gap = words[index + 1].start - words[index].end if index + 1 < len(words) else duration - words[index].end
            if max(before_gap, after_gap) >= 0.22 and words[index].end - words[index].start <= 0.70:
                phrase_length = 1

        if not phrase_length:
            index += 1
            continue

        first = words[index]
        last = words[index + phrase_length - 1]
        previous_end = words[index - 1].end if index else 0.0
        next_start = words[index + phrase_length].start if index + phrase_length < len(words) else duration
        left_room = max(0.0, first.start - previous_end)
        right_room = max(0.0, next_start - last.end)
        start = max(0.0, first.start - min(0.055, left_room * 0.45))
        end = min(duration, last.end + min(0.075, right_room * 0.45))
        if use_audio:
            outer_limit = next_start
            for segment_start, segment_end in speech_segments or ():
                if segment_start - 0.05 <= first.start <= segment_end:
                    outer_limit = min(outer_limit, segment_end + 0.05)
                    break
            sound_start, sound_end = _filler_extent(levels, first.start, outer_limit)
            start = sound_start if sound_start is not None else first.start + 0.02
            # Over game audio a quiet "mm" can sink to the background level, so the
            # audio alone may end the cut early; the transcript's end of the filler
            # (never closer than 50 ms to the next word) keeps it from falling short.
            transcript_end = min(last.end, next_start - 0.05)
            end = transcript_end if sound_end is None else max(sound_end, transcript_end)
            start, end = max(0.0, start), min(duration, end)
            if end - start < 0.06:
                index += phrase_length
                continue
        intervals.append((start, end))
        removed_words += phrase_length
        index += phrase_length

    return _merge_cut_intervals(intervals, duration), removed_words


# Pauses are gaps in the creator's voice, not digital silence. Stream VODs keep game
# audio, music, and mic hiss running between sentences, so a fixed silence level never
# fires on them. The detector below tracks the recording's own background level and
# speech level and marks speech relative to both.
_SPEECH_SAMPLE_RATE = 16_000
_SPEECH_HOP_SAMPLES = 160  # 10 ms
_SPEECH_WINDOW_SAMPLES = 400  # 25 ms
_SPEECH_FFT_SIZE = 512
_SPEECH_BAND_HZ = (200.0, 7_000.0)  # keeps the fricatives at the ends of words
_SPEECH_BLOCK_FRAMES = 25  # background and speech levels are tracked per 0.25 s
_SPEECH_CONTEXT_BLOCKS = 8  # compared across roughly four seconds either side
_SPEECH_CACHE: dict[tuple[object, ...], np.ndarray] = {}
_SPEECH_SEGMENT_CACHE: dict[tuple[object, ...], tuple[tuple[float, float], ...]] = {}


def _speech_frame_seconds() -> float:
    return _SPEECH_HOP_SAMPLES / _SPEECH_SAMPLE_RATE


def _speech_band_levels(source: Path, start: float, end: float) -> np.ndarray:
    """Voice-band level in dB for every 10 ms, decoded as a stream in bounded memory."""
    duration = max(0.1, float(end) - float(start))
    cache_key = _analysis_cache_key(source, max(0.0, start), max(start + 0.1, end), "speech-levels")
    with _FULL_LENGTH_ANALYSIS_CACHE_LOCK:
        cached = _SPEECH_CACHE.get(cache_key)
        if cached is not None:
            return cached
    command = [
        ffmpeg_executable(), "-v", "error",
        "-ss", f"{max(0.0, float(start)):.3f}", "-i", str(audio_source(source)),
        "-t", f"{duration:.3f}", "-vn", "-ac", "1", "-ar", str(_SPEECH_SAMPLE_RATE),
        "-f", "s16le", "pipe:1",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if process.stdout is None:
        raise RuntimeError("FFmpeg did not provide decoded audio.")
    window = np.hanning(_SPEECH_WINDOW_SAMPLES).astype(np.float32)
    frequencies = np.fft.rfftfreq(_SPEECH_FFT_SIZE, 1.0 / _SPEECH_SAMPLE_RATE)
    band = (frequencies >= _SPEECH_BAND_HZ[0]) & (frequencies <= _SPEECH_BAND_HZ[1])
    offsets = np.arange(_SPEECH_WINDOW_SAMPLES)
    chunk_bytes = _SPEECH_SAMPLE_RATE * np.dtype(np.int16).itemsize
    carry = np.zeros(0, dtype=np.float32)
    levels: list[np.ndarray] = []
    try:
        while True:
            raw = _read_exactly(process.stdout, chunk_bytes)
            if not raw:
                break
            usable = len(raw) - (len(raw) % 2)
            fresh = np.frombuffer(raw[:usable], dtype=np.int16).astype(np.float32) / 32768.0
            samples = np.concatenate([carry, fresh]) if carry.size else fresh
            frame_count = (samples.size - _SPEECH_WINDOW_SAMPLES) // _SPEECH_HOP_SAMPLES + 1
            if frame_count > 0:
                indexes = np.arange(frame_count)[:, None] * _SPEECH_HOP_SAMPLES + offsets[None, :]
                spectrum = np.fft.rfft(samples[indexes] * window, n=_SPEECH_FFT_SIZE)
                power = np.square(np.abs(spectrum[:, band])).sum(axis=1)
                levels.append((10.0 * np.log10(power + 1e-10)).astype(np.float32))
                carry = samples[frame_count * _SPEECH_HOP_SAMPLES:]
            else:
                carry = samples
            if len(raw) < chunk_bytes:
                break
    finally:
        process.stdout.close()
        process.wait()
    result = np.concatenate(levels) if levels else np.zeros(0, dtype=np.float32)
    with _FULL_LENGTH_ANALYSIS_CACHE_LOCK:
        _store_bounded_cache(_SPEECH_CACHE, cache_key, result)
    return result


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Start and end (exclusive) frame of every run of True values."""
    if mask.size == 0:
        return []
    padded = np.concatenate([[False], mask, [False]])
    edges = np.flatnonzero(np.diff(padded.astype(np.int8)))
    return list(zip(edges[0::2].tolist(), edges[1::2].tolist()))


def _rolling_extreme(values: np.ndarray, radius: int, use_max: bool) -> np.ndarray:
    padded = np.pad(values, radius, mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, 2 * radius + 1)
    return windows.max(axis=1) if use_max else windows.min(axis=1)


def _speech_activity(levels: np.ndarray) -> np.ndarray:
    """Mark the 10 ms frames where someone is speaking.

    The background level is the quietest stretch nearby and the speech level the
    loudest; a frame is speech when it rises well above the background relative to
    that contrast, so the same rule works in a quiet room and over loud gameplay.
    """
    count = int(levels.size)
    if count == 0:
        return np.zeros(0, dtype=bool)
    block = _SPEECH_BLOCK_FRAMES
    block_count = -(-count // block)
    padded = np.pad(levels, (0, block_count * block - count), mode="edge").reshape(block_count, block)
    background = _rolling_extreme(np.percentile(padded, 20, axis=1), _SPEECH_CONTEXT_BLOCKS, use_max=False)
    peak = _rolling_extreme(np.percentile(padded, 95, axis=1), _SPEECH_CONTEXT_BLOCKS, use_max=True)
    background = np.repeat(background, block)[:count]
    contrast = np.maximum(0.0, np.repeat(peak, block)[:count] - background)
    enter = levels > background + np.maximum(7.0, 0.45 * contrast)
    stay = levels > background + np.maximum(4.0, 0.30 * contrast)
    # Hysteresis: a stretch above the lower line counts only if it also crosses the higher one.
    speech = np.zeros(count, dtype=bool)
    for first, last in _runs(stay):
        if enter[first:last].any():
            speech[first:last] = True
    # Close the short dips inside and between words, then drop blips too short to be speech.
    frame = _speech_frame_seconds()
    for first, last in _runs(~speech):
        if 0 < first and last < count and (last - first) * frame < 0.15:
            speech[first:last] = True
    for first, last in _runs(speech):
        if (last - first) * frame < 0.12:
            speech[first:last] = False
    return speech


def _tighten_speech_edges(
    segments: list[tuple[float, float]],
    levels: np.ndarray,
    duration: float,
    start_reach: float = 0.35,
    start_margin_db: float = 6.0,
    end_reach: float = 0.25,
    end_margin_db: float = 3.0,
) -> list[tuple[float, float]]:
    """Trim the padding Silero leaves around speech, using the voice-band level.

    Silero decides where the pauses are; this only moves each segment edge inward
    to where the voice actually rises above the background. Words start sharply but
    trail off, so the end of speech is trimmed more gently than the start, keeping
    soft final sounds such as the "s" in "closes" that fade under game audio.
    """
    if not segments or levels.size == 0:
        return segments
    frame = _speech_frame_seconds()
    centre = _SPEECH_WINDOW_SAMPLES / (2 * _SPEECH_SAMPLE_RATE)

    def index_at(seconds: float) -> int:
        return int(min(levels.size - 1, max(0, round((seconds - centre) / frame))))

    def background_near(gap_start: float, gap_end: float) -> float | None:
        first, last = index_at(gap_start), index_at(gap_end)
        if last - first < 8:
            return None
        return float(np.median(levels[first:last]))

    tightened: list[tuple[float, float]] = []
    start_frames = int(round(start_reach / frame))
    end_frames = int(round(end_reach / frame))
    for position, (segment_start, segment_end) in enumerate(segments):
        before = segments[position - 1][1] if position else 0.0
        after = segments[position + 1][0] if position + 1 < len(segments) else duration
        new_start, new_end = segment_start, segment_end
        floor = background_near(before, segment_start)
        if floor is not None:
            first = index_at(segment_start)
            window = levels[first:first + start_frames]
            loud = np.flatnonzero(window > floor + start_margin_db)
            if loud.size:
                new_start = max(segment_start, (first + loud[0]) * frame + centre - 0.03)
        floor = background_near(segment_end, after)
        if floor is not None:
            last = index_at(segment_end)
            window = levels[max(0, last - end_frames):last + 1]
            loud = np.flatnonzero(window > floor + end_margin_db)
            if loud.size:
                new_end = min(segment_end, (last - (window.size - 1 - loud[-1])) * frame + centre + 0.03)
        if new_end - new_start >= 0.08:
            tightened.append((round(float(new_start), 3), round(float(new_end), 3)))
        else:
            tightened.append((segment_start, segment_end))
    return tightened


def _speech_segments(source: Path, start: float, end: float) -> list[tuple[float, float]]:
    """Where someone is speaking, from the bundled Silero model when available.

    Builds without the model (a source checkout, for example) fall back to the
    loudness-based detector above, which is weaker over loud game audio.
    """
    duration = max(0.1, float(end) - float(start))
    cache_key = _analysis_cache_key(source, max(0.0, start), max(start + 0.1, end), "speech-segments")
    with _FULL_LENGTH_ANALYSIS_CACHE_LOCK:
        cached = _SPEECH_SEGMENT_CACHE.get(cache_key)
        if cached is not None:
            return list(cached)
    try:
        segments = detect_speech_segments(audio_source(source), start, end, ffmpeg_executable())
    except (OSError, RuntimeError, ValueError):
        segments = None
    levels = _speech_band_levels(source, start, end)
    if segments is not None:
        result = _tighten_speech_edges(segments, levels, duration)
        with _FULL_LENGTH_ANALYSIS_CACHE_LOCK:
            _store_bounded_cache(_SPEECH_SEGMENT_CACHE, cache_key, tuple(result))
        return result
    speech = _speech_activity(levels)
    frame = _speech_frame_seconds()
    centre = _SPEECH_WINDOW_SAMPLES / (2 * _SPEECH_SAMPLE_RATE)
    return [
        (round(first * frame + centre, 3), round(min(duration, last * frame + centre), 3))
        for first, last in _runs(speech)
    ]


def _speech_context(
    source: Path, start: float, end: float
) -> tuple[np.ndarray | None, list[tuple[float, float]] | None]:
    """Voice-band levels and speech segments for placing filler cuts, when decodable."""
    try:
        return _speech_band_levels(source, start, end), _speech_segments(source, start, end)
    except (OSError, RuntimeError):
        return None, None


# A gap in speech up to this long is a pause between sentences and is tightened.
# A longer gap is usually content -- gameplay, a song, a moment of concentration --
# so only its genuinely quiet parts are removed.
_LONGEST_SPEECH_PAUSE = 3.0
# "Genuinely quiet" means at least this far below the level of the speech itself.
_DEAD_AIR_DB = 30.0


def _level_index(levels: np.ndarray) -> Callable[[float], int]:
    """Map a time in seconds to its frame in a speech-band level array."""
    frame = _speech_frame_seconds()
    centre = _SPEECH_WINDOW_SAMPLES / (2 * _SPEECH_SAMPLE_RATE)
    return lambda seconds: int(min(levels.size, max(0, round((seconds - centre) / frame))))


def _speech_reference_level(levels: np.ndarray, segments: list[tuple[float, float]]) -> float:
    """The typical speech level, or the loudest audio's when nobody talks."""
    index_at = _level_index(levels)
    spoken = np.concatenate([levels[index_at(a):index_at(b)] for a, b in segments] or [levels[:0]])
    return float(np.median(spoken)) if spoken.size else float(np.percentile(levels, 90))


def _dead_air(
    levels: np.ndarray,
    segments: list[tuple[float, float]],
    gap_start: float,
    gap_end: float,
    minimum: float,
) -> list[tuple[float, float]]:
    """Stretches inside a gap that are silent compared with the speech.

    With no speech at all (music, or gameplay nobody talks over), silence is judged
    against the loudest audio in the selection instead.
    """
    if levels.size == 0:
        return []
    frame = _speech_frame_seconds()
    centre = _SPEECH_WINDOW_SAMPLES / (2 * _SPEECH_SAMPLE_RATE)
    index_at = _level_index(levels)
    quiet = levels < _speech_reference_level(levels, segments) - _DEAD_AIR_DB
    first, last = index_at(gap_start), index_at(gap_end)
    stretches: list[tuple[float, float]] = []
    for run_start, run_end in _runs(quiet[first:last]):
        a, b = (first + run_start) * frame + centre, (first + run_end) * frame + centre
        if b - a >= minimum:
            stretches.append((max(gap_start, a), min(gap_end, b)))
    return stretches


def _speech_pause_cut_intervals(
    source: Path,
    start: float,
    end: float,
    minimum_pause: float = 0.40,
    tail_keep: float = 0.15,
    lead_keep: float = 0.10,
) -> list[tuple[float, float]]:
    """Find removable pauses in speech, keeping a short natural breath at each edge.

    Gaps between sentences up to three seconds are tightened whatever is playing
    underneath. Longer gaps, and the stretches before the first word and after the
    last, keep their audible content and lose only dead air.
    `tail_keep` is left after speech ends and `lead_keep` before it resumes, so a
    tightened pause is about a quarter of a second, like a natural breath.
    """
    duration = max(0.1, float(end) - float(start))
    cache_key = _analysis_cache_key(
        source, max(0.0, start), max(start + 0.1, end), "speech-pauses",
        round(minimum_pause, 3), round(tail_keep, 3), round(lead_keep, 3),
        _LONGEST_SPEECH_PAUSE, _DEAD_AIR_DB,
    )
    with _FULL_LENGTH_ANALYSIS_CACHE_LOCK:
        cached = _SILENCE_CUT_CACHE.get(cache_key)
        if cached is not None:
            return list(cached)
    try:
        segments = _speech_segments(source, start, end)
        levels = _speech_band_levels(source, start, end)
    except (OSError, RuntimeError):
        return []
    # The gaps between speech, including dead air before the first word and after the last.
    boundaries = [0.0]
    for segment_start, segment_end in segments:
        boundaries.extend([segment_start, segment_end])
    boundaries.append(duration)
    cuts: list[tuple[float, float]] = []
    for index in range(0, len(boundaries), 2):
        gap_start, gap_end = boundaries[index], min(duration, boundaries[index + 1])
        if gap_end - gap_start < minimum_pause:
            continue
        opening, closing = index == 0, index + 2 >= len(boundaries)
        # Only a gap between two stretches of speech is a pause between sentences.
        # Before the first word or after the last it may be an intro or gameplay.
        if not opening and not closing and gap_end - gap_start <= _LONGEST_SPEECH_PAUSE:
            stretches = [(gap_start, gap_end)]
        else:
            stretches = _dead_air(levels, segments, gap_start, gap_end, minimum_pause)
        for stretch_start, stretch_end in stretches:
            # Keep a breath beside speech; dead air before the first word or after the
            # last keeps a little less, and quiet inside a long gap keeps the same.
            touches_speech_before = stretch_start <= gap_start + 0.01 and not opening
            touches_speech_after = stretch_end >= gap_end - 0.01 and not closing
            left_keep = tail_keep if touches_speech_before else 0.08
            right_keep = lead_keep if touches_speech_after else 0.08
            cut_start, cut_end = stretch_start + left_keep, stretch_end - right_keep
            if cut_end - cut_start >= 0.15:
                cuts.append((cut_start, cut_end))
    merged = _merge_cut_intervals(cuts, duration)
    with _FULL_LENGTH_ANALYSIS_CACHE_LOCK:
        _store_bounded_cache(_SILENCE_CUT_CACHE, cache_key, tuple(merged))
    return merged


# Still scenes: stretches where the picture barely changes, such as a lobby or map
# screen, a BRB card, or a phone left filming while the streamer rests. Frames are
# sampled four times a second at 96x54 in grey. A stretch stays still while no
# more than 5% of the frame differs from its first frame, so a moving face cam,
# a chat box, or a cursor does not break it, but a slow pan eventually does.
# Calibrated on stream VODs: Mario Kart lobbies, gym rest breaks between sets,
# and reaction-stream title cards come out still; driving footage does not.
_STILL_SAMPLE_FPS = 4
_STILL_WIDTH, _STILL_HEIGHT = 96, 54
_STILL_PIXEL_CHANGE = 12          # Grey levels a pixel must move to count as changed.
_STILL_AREA_LIMIT = 0.05          # Share of the frame that may change while still.
_STILL_MIN_SECONDS = 5.0
_STILL_KEEP_START = 1.0           # Kept so each still scene still registers.
_STILL_KEEP_END = 0.5
_STILL_SPEECH_MARGIN = 0.30       # Kept around speech inside a still stretch.
_STILL_CACHE: dict[tuple[object, ...], tuple[tuple[float, float], ...]] = {}


def _still_stretches_in_frames(frames, frames_per_second: float = _STILL_SAMPLE_FPS) -> list[tuple[float, float]]:
    """Still stretches, in seconds, in an iterable of equally spaced grey frames.

    Each frame is compared with the first frame of the stretch it may belong to;
    a single odd frame, such as a flash or a keyframe pulse, does not end it.
    """
    stretches: list[tuple[float, float]] = []
    anchor: np.ndarray | None = None
    anchor_index = last_still = strikes = 0
    index = -1
    for index, frame in enumerate(frames):
        frame = np.asarray(frame, dtype=np.int16)
        if anchor is None:
            anchor, anchor_index, last_still = frame, index, index
            continue
        changed = float(np.mean(np.abs(frame - anchor) > _STILL_PIXEL_CHANGE))
        if changed <= _STILL_AREA_LIMIT:
            last_still, strikes = index, 0
            continue
        strikes += 1
        if strikes < 2:
            continue
        if (last_still - anchor_index) / frames_per_second >= _STILL_MIN_SECONDS:
            stretches.append((anchor_index / frames_per_second, last_still / frames_per_second))
        anchor, anchor_index, last_still, strikes = frame, index, index, 0
    if anchor is not None and (last_still - anchor_index) / frames_per_second >= _STILL_MIN_SECONDS:
        stretches.append((anchor_index / frames_per_second, last_still / frames_per_second))
    return stretches


def _still_stretches(source: Path, start: float, end: float) -> list[tuple[float, float]]:
    """Still stretches in a selection, relative to its start, decoded as a stream."""
    duration = max(0.1, float(end) - float(start))
    cache_key = _analysis_cache_key(
        source, max(0.0, start), max(start + 0.1, end), "still-scenes",
        _STILL_SAMPLE_FPS, _STILL_PIXEL_CHANGE, _STILL_AREA_LIMIT, _STILL_MIN_SECONDS,
    )
    with _FULL_LENGTH_ANALYSIS_CACHE_LOCK:
        cached = _STILL_CACHE.get(cache_key)
        if cached is not None:
            return list(cached)
    frame_bytes = _STILL_WIDTH * _STILL_HEIGHT
    process = subprocess.Popen(
        [
            # Skipping the deblocking filter decodes about a fifth faster and cannot
            # matter at this size.
            ffmpeg_executable(), "-v", "error", "-skip_loop_filter", "all",
            "-ss", f"{max(0.0, float(start)):.3f}", "-t", f"{duration:.3f}", "-i", str(source), "-an",
            "-vf", f"fps={_STILL_SAMPLE_FPS},scale={_STILL_WIDTH}:{_STILL_HEIGHT}:flags=area,format=gray",
            "-f", "rawvideo", "pipe:1",
        ],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )

    def frames():
        while True:
            data = _read_exactly(process.stdout, frame_bytes)
            if len(data) < frame_bytes:
                return
            yield np.frombuffer(data, dtype=np.uint8).reshape(_STILL_HEIGHT, _STILL_WIDTH)

    try:
        stretches = [
            (round(a, 3), round(min(b, duration), 3)) for a, b in _still_stretches_in_frames(frames())
        ]
    finally:
        process.kill()
        process.stdout.close()
        process.wait()
    with _FULL_LENGTH_ANALYSIS_CACHE_LOCK:
        _store_bounded_cache(_STILL_CACHE, cache_key, tuple(stretches))
    return stretches


def _still_scene_cut_intervals(
    stretches: list[tuple[float, float]],
    speech_segments: list[tuple[float, float]] | None,
    words: list[CaptionWord] | tuple[CaptionWord, ...],
    levels: np.ndarray | None,
    duration: float,
) -> list[tuple[float, float]]:
    """Cut still stretches down to a moment at each edge, never while anyone speaks."""
    cuts = [
        (a + _STILL_KEEP_START, b - _STILL_KEEP_END)
        for a, b in stretches
        if b - _STILL_KEEP_END - (a + _STILL_KEEP_START) >= 1.0
    ]
    if not cuts:
        return []
    # Speech inside a still stretch stays, with a little room either side: a
    # streamer talking over a paused game or an empty room is still content.
    spoken = [
        (a - _STILL_SPEECH_MARGIN, b + _STILL_SPEECH_MARGIN) for a, b in speech_segments or []
    ]
    kept: list[tuple[float, float]] = []
    for cut_start, cut_end in cuts:
        pieces = [(cut_start, cut_end)]
        for speech_start, speech_end in spoken:
            pieces = [
                piece
                for piece_start, piece_end in pieces
                for piece in (
                    [(piece_start, piece_end)]
                    if speech_end <= piece_start or speech_start >= piece_end
                    else [(piece_start, min(piece_end, speech_start)), (max(piece_start, speech_end), piece_end)]
                )
            ]
        kept.extend(piece for piece in pieces if piece[1] - piece[0] >= 1.0)
    # Words the speech detector missed are spared the same way as in pause removal.
    return _spare_spoken_words(
        _merge_cut_intervals(kept, duration), words, levels, speech_segments, duration,
        tail_keep=_STILL_SPEECH_MARGIN, lead_keep=_STILL_SPEECH_MARGIN,
    )


def _spare_spoken_words(
    cuts: list[tuple[float, float]],
    words: list[CaptionWord] | tuple[CaptionWord, ...],
    levels: np.ndarray | None,
    speech_segments: list[tuple[float, float]] | None,
    duration: float,
    tail_keep: float = 0.15,
    lead_keep: float = 0.10,
) -> list[tuple[float, float]]:
    """Take words the speech detector missed back out of pause cuts.

    The detector now and then misses a word, typically the first or last one of a
    phrase said over game audio, and a short gap between sentences is cut whole,
    word and all. A word Whisper heard that starts inside such a gap, is not a
    filler or part of a sound tag such as "[sounds of running]", and is not
    silent keeps the same breath around it as detected speech; the rest of the
    pause is still cut. Whisper's confidence does not tell real words from
    imagined ones here (its "Okay." over silence scores 0.9), but silence does.

    Only the start of a word is trusted: Whisper's DTW places it closely, while
    its end is an estimate, so a word that starts inside detected speech is left
    to the detector's edges. Protecting estimated ends kept 26% of benchmark
    pauses instead of 9%; protecting starts changed nothing there.
    """
    if not cuts or not words:
        return cuts
    segments = sorted(speech_segments or [])
    segment_starts = [start for start, _ in segments]
    index_at = _level_index(levels) if levels is not None and levels.size else None
    silent_below = (
        _speech_reference_level(levels, speech_segments or []) - _DEAD_AIR_DB if index_at else None
    )
    spared: list[tuple[float, float]] = []
    tag_words = 0  # Words so far in an open tag; an unclosed tag ends after eight.
    for word in words:
        text = word.text.strip()
        tag = 0 < tag_words < 8 or text[:1] in {"[", "(", "*", "♪"}
        tag_words = tag_words + 1 if tag and text[-1:] not in {"]", ")", "*", "♪"} else 0
        if tag or _normalized_caption_word(word) in FILLER_WORDS or not _normalized_caption_word(word):
            continue
        following = bisect.bisect_right(segment_starts, word.start)
        if following and word.start < segments[following - 1][1]:
            continue  # Inside detected speech.
        if index_at is not None:
            window = levels[index_at(word.start):max(index_at(word.start) + 1, index_at(word.end))]
            if window.size and float(np.max(window)) < silent_below:
                continue
        spared.append((word.start - lead_keep, word.end + tail_keep))
    if not spared:
        return cuts
    spared.sort()
    trimmed: list[tuple[float, float]] = []
    for cut_start, cut_end in cuts:
        pieces = [(cut_start, cut_end)]
        for spare_start, spare_end in spared:
            if spare_start >= cut_end:
                break
            if spare_end <= cut_start:
                continue
            pieces = [
                piece
                for piece_start, piece_end in pieces
                for piece in (
                    [(piece_start, piece_end)]
                    if spare_end <= piece_start or spare_start >= piece_end
                    else [(piece_start, min(piece_end, spare_start)), (max(piece_start, spare_end), piece_end)]
                )
            ]
        trimmed.extend((start, end) for start, end in pieces if end - start >= 0.15)
    return _merge_cut_intervals(trimmed, duration)


def _kept_segments(cuts: list[tuple[float, float]], duration: float) -> list[tuple[float, float]]:
    segments: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in _merge_cut_intervals(cuts, duration):
        if start - cursor >= 0.08:
            segments.append((cursor, start))
        cursor = max(cursor, end)
    if duration - cursor >= 0.08:
        segments.append((cursor, duration))
    if not segments:
        return [(0.0, duration)]
    return [(round(start, 3), round(end, 3)) for start, end in segments]


def _collapsed_timeline_time(value: float, cuts: list[tuple[float, float]]) -> float:
    """Map an original relative timestamp onto the timeline after cuts."""
    timestamp = max(0.0, float(value))
    removed = sum(max(0.0, min(timestamp, end) - start) for start, end in cuts if timestamp > start)
    return max(0.0, timestamp - removed)


def _time_is_removed(value: float, cuts: list[tuple[float, float]]) -> bool:
    timestamp = float(value)
    return any(start <= timestamp <= end for start, end in cuts)


def _remap_transcript_after_cuts(
    words: list[CaptionWord] | tuple[CaptionWord, ...],
    cuts: list[tuple[float, float]],
    duration: float,
) -> list[CaptionWord]:
    """Keep spoken-word timing aligned with the single-pass cleaned timeline."""
    merged = _merge_cut_intervals(cuts, duration)
    output_duration = max(0.0, duration - sum(end - start for start, end in merged))
    remapped: list[CaptionWord] = []
    for word in words:
        new_start = min(output_duration, _collapsed_timeline_time(word.start, merged))
        new_end = min(output_duration, _collapsed_timeline_time(word.end, merged))
        if new_end - new_start < 0.025:
            continue
        if remapped and new_start < remapped[-1].start:
            continue
        remapped.append(CaptionWord(word.text, round(new_start, 3), round(new_end, 3)))
    return remapped


def _cut_audio_envelope(
    envelope: np.ndarray,
    hop_seconds: float,
    cuts: list[tuple[float, float]],
    duration: float,
) -> np.ndarray:
    """Collapse analysis samples without decoding a second temporary video."""
    if not cuts:
        return envelope
    pieces: list[np.ndarray] = []
    for segment_start, segment_end in _kept_segments(cuts, duration):
        first = max(0, int(math.floor(segment_start / hop_seconds)))
        last = min(len(envelope), int(math.ceil(segment_end / hop_seconds)))
        if last > first:
            pieces.append(envelope[first:last])
    if not pieces:
        return np.zeros(1, dtype=np.float32)
    return np.concatenate(pieces).astype(np.float32, copy=False)


# Stream VODs can change picture size or audio channels partway through, for
# example when the streamer switches their output from 720p to 1080p. FFmpeg
# normally rebuilds the filter graph at each switch, which restarts any trim or
# concat inside it: an edit either fails or silently ends at the switch. Edits
# therefore never let the graph rebuild. The video runs through one chain that
# picks the kept frames by time and scales every frame to the output size (the
# scaler follows size changes by itself). The audio is decoded by a separate
# process, where a rebuild is harmless, and cut in Python.
_STEADY_INPUT = ("-reinit_filter", "0")
_EDIT_SAMPLE_RATE = 48_000
_EDIT_CHANNELS = 2
_EDIT_FADE_SECONDS = 0.012
# The edited audio waits in a fast, lossless FLAC: an hour takes a few seconds and
# about 200 MB, where encoding AAC up front would add a minute before the picture.
_LOSSLESS_AUDIO = ("-c:a", "flac", "-compression_level", "0", "-sample_fmt", "s16")
# Longer filter graphs go through a file: Windows caps a command line at 32,767 characters.
_INLINE_GRAPH_LIMIT = 6_000
_FFMPEG_MAJOR_VERSIONS: dict[str, int] = {}


def _fit_frame(width: int, height: int) -> str:
    """Filters that bring every frame, whatever its size, to one output frame."""
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={width}:{height},setsar=1"
    )


def _gaps_between(segments: list[tuple[float, float]], duration: float) -> list[tuple[float, float]]:
    """The removed stretches of a timeline that keeps only these segments."""
    gaps: list[tuple[float, float]] = []
    cursor = 0.0
    for segment_start, segment_end in segments:
        if segment_start > cursor:
            gaps.append((round(cursor, 3), round(segment_start, 3)))
        cursor = max(cursor, segment_end)
    if duration > cursor:
        gaps.append((round(cursor, 3), round(duration, 3)))
    return [(start, end) for start, end in gaps if end > start]


def _kept_frames_filter(segments: list[tuple[float, float]], duration: float) -> str:
    """Keep only the frames inside the segments and close the gaps between them.

    One select and one setpts replace a trim per segment joined by concat: the
    graph stays two filters long however many cuts there are, and a frame keeps
    its exact place on the edited timeline, T minus everything removed before it.
    """
    if len(segments) == 1 and segments[0][0] <= 0.0005 and segments[0][1] >= duration - 0.0005:
        return "setpts=PTS-STARTPTS"
    keep = "+".join(f"gte(t,{start:.4f})*lt(t,{end:.4f})" for start, end in segments)
    steps: list[str] = []
    previous_end = 0.0
    for start, end in segments:
        if start - previous_end > 0.00005:
            steps.append(f"{start - previous_end:.4f}*gte(T,{start:.4f})")
        previous_end = end
    return f"select='{keep}',setpts='(T-({'+'.join(steps) or '0'}))/TB'"


def _ffmpeg_major_version(ffmpeg: str) -> int:
    if ffmpeg not in _FFMPEG_MAJOR_VERSIONS:
        try:
            banner = subprocess.run(
                [ffmpeg, "-version"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=20
            ).stdout
        except (OSError, subprocess.SubprocessError):
            banner = ""
        match = re.search(r"ffmpeg version n?(\d+)\.", banner)
        # Builds from FFmpeg's main branch report "N-<commit>" and are newer than any release.
        _FFMPEG_MAJOR_VERSIONS[ffmpeg] = int(match.group(1)) if match else 99
    return _FFMPEG_MAJOR_VERSIONS[ffmpeg]


def _filter_graph_args(ffmpeg: str, graph: str, temporary_paths: list[Path]) -> list[str]:
    """Pass a filter graph on the command line, or in a file when it is long."""
    if len(graph) <= _INLINE_GRAPH_LIMIT:
        return ["-filter_complex", graph]
    handle = tempfile.NamedTemporaryFile(
        "w", prefix=f"{APP_SLUG}-graph-", suffix=".txt", delete=False, encoding="utf-8"
    )
    handle.write(graph)
    handle.close()
    temporary_paths.append(Path(handle.name))
    # FFmpeg 7 replaced -filter_complex_script with -/filter_complex; FFmpeg 8 removed the old form.
    option = "-/filter_complex" if _ffmpeg_major_version(ffmpeg) >= 7 else "-filter_complex_script"
    return [option, handle.name]


def _steady_audio_command(ffmpeg: str, source: Path, start: float, duration: float) -> list[str]:
    """Decode a section's audio as raw 48 kHz stereo, whatever format changes it has."""
    return [
        ffmpeg, "-v", "error",
        "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(audio_source(source)),
        "-map", "0:a:0", "-vn",
        # async fills any real timestamp gap with silence so the audio stays in
        # step with the video. A forced first_pts would pad again after every
        # format change, so it is left unset.
        "-af", "aresample=async=1",
        "-ac", str(_EDIT_CHANNELS), "-ar", str(_EDIT_SAMPLE_RATE), "-f", "s16le", "-",
    ]


def _copy_kept_audio(reader, writer, segments: list[tuple[float, float]]) -> bool:
    """Copy the kept segments of a raw 16-bit stereo stream, fading each join.

    Every segment fades in and out over 12 ms so the joins never click. Returns
    True when it stopped after the last segment, before the stream ended.
    """
    frame_bytes = 2 * _EDIT_CHANNELS
    kept = [
        (round(start * _EDIT_SAMPLE_RATE), round(end * _EDIT_SAMPLE_RATE))
        for start, end in segments
    ]
    kept = [(start, end) for start, end in kept if end > start]
    position = 0
    index = 0
    while index < len(kept):
        data = reader.read(_EDIT_SAMPLE_RATE * frame_bytes)
        count = len(data) // frame_bytes
        if count == 0:
            return False
        samples = np.frombuffer(data, dtype="<i2", count=count * _EDIT_CHANNELS).reshape(count, _EDIT_CHANNELS)
        block_end = position + count
        while index < len(kept) and kept[index][0] < block_end:
            start, end = kept[index]
            low, high = max(start, position), min(end, block_end)
            if high > low:
                piece = samples[low - position: high - position]
                fade = max(1, min(round(_EDIT_FADE_SECONDS * _EDIT_SAMPLE_RATE), (end - start) // 4))
                if low - start < fade or end - high < fade:
                    offsets = np.arange(low - start, high - start, dtype=np.float64)
                    gain = np.minimum(1.0, np.minimum((offsets + 1.0) / fade, (end - start - offsets) / fade))
                    piece = np.clip(np.rint(piece * gain[:, None]), -32768, 32767).astype("<i2")
                writer.write(piece.tobytes())
            if end > block_end:
                break
            index += 1
        position = block_end
    return True


def _run_with_kept_audio(
    command: list[str],
    source: Path,
    start: float,
    duration: float,
    segments: list[tuple[float, float]],
) -> None:
    """Run an FFmpeg command that reads the edit's audio as raw PCM on stdin."""
    with tempfile.TemporaryFile() as decode_errors, tempfile.TemporaryFile() as encode_errors:
        decoder = subprocess.Popen(
            _steady_audio_command(command[0], source, start, duration),
            stdout=subprocess.PIPE, stderr=decode_errors,
        )
        read_to_end = False
        try:
            encoder = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=encode_errors
            )
            try:
                try:
                    read_to_end = not _copy_kept_audio(decoder.stdout, encoder.stdin, segments)
                except BrokenPipeError:
                    pass  # The encoder stopped early; its exit status says why.
                finally:
                    try:
                        encoder.stdin.close()
                    except BrokenPipeError:
                        pass
                encoder_status = encoder.wait()
            except BaseException:
                encoder.kill()
                encoder.wait()
                raise
        finally:
            # Past the last kept segment, or after a failure, the rest of the
            # source's audio is not needed.
            if not read_to_end:
                decoder.kill()
            decoder.stdout.close()
            decoder_status = decoder.wait()
        if encoder_status != 0:
            encode_errors.seek(0)
            raise subprocess.CalledProcessError(encoder_status, command, stderr=encode_errors.read())
        if read_to_end and decoder_status != 0:
            decode_errors.seek(0)
            raise subprocess.CalledProcessError(decoder_status, decoder.args, stderr=decode_errors.read())


def _render_kept_segments(
    source: Path,
    output: Path,
    selection_start: float,
    segments: list[tuple[float, float]],
) -> float:
    """Join retained sections into one smooth, local full-length edit."""
    info = probe_video(source)
    width, height = info.width - info.width % 2, info.height - info.height % 2
    duration = segments[-1][1]
    total_duration = round(sum(end - start for start, end in segments), 3)
    ffmpeg = ffmpeg_executable()
    temporary_paths: list[Path] = []
    try:
        audio_track: Path | None = None
        if _has_audio(source):
            handle = tempfile.NamedTemporaryFile(prefix=f"{APP_SLUG}-audio-", suffix=".flac", delete=False)
            audio_track = Path(handle.name)
            handle.close()
            temporary_paths.append(audio_track)
            _run_with_kept_audio(
                [
                    ffmpeg, "-y", "-v", "error",
                    "-f", "s16le", "-ar", str(_EDIT_SAMPLE_RATE), "-ac", str(_EDIT_CHANNELS), "-i", "pipe:0",
                    "-af", f"apad=whole_dur={total_duration:.3f}", "-t", f"{total_duration:.3f}",
                    *_LOSSLESS_AUDIO, str(audio_track),
                ],
                source, selection_start, duration, segments,
            )
        inputs = [
            ffmpeg, "-y", "-v", "error", *_STEADY_INPUT,
            "-ss", f"{selection_start:.3f}", "-t", f"{duration:.3f}", "-i", str(source),
        ]
        if audio_track is not None:
            inputs += ["-i", str(audio_track)]
        graph = _filter_graph_args(
            ffmpeg,
            f"[0:v]{_kept_frames_filter(segments, duration)},{_fit_frame(width, height)}[outv]",
            temporary_paths,
        )

        def command_for(codec: list[str]) -> list[str]:
            command = [*inputs, *graph, "-map", "[outv]"]
            if audio_track is not None:
                command += ["-map", "1:a", "-c:a", "aac", "-b:a", "192k"]
            return command + [
                "-t", f"{total_duration:.3f}", *codec,
                *_frame_rate_args(info.frame_rate), "-movflags", "+faststart", str(output),
            ]

        _encode_video(command_for, width, height, info.frame_rate, crf=19)
    finally:
        for temporary_path in temporary_paths:
            temporary_path.unlink(missing_ok=True)
    return total_duration


def _prepare_full_length_source(
    source: Path,
    output: Path,
    start: float,
    end: float,
    remove_silence: bool,
    remove_filler_words: bool,
) -> dict[str, object]:
    duration = max(0.1, float(end) - float(start))
    silence_cuts = _speech_pause_cut_intervals(source, start, end) if remove_silence else []
    filler_cuts: list[tuple[float, float]] = []
    filler_count = 0
    if remove_filler_words:
        try:
            transcript_words = _transcribe_words_cached(source, start, end, include_fillers=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(f"Filler-word removal needs the bundled offline speech engine: {exc}") from exc
        silence_cuts = _spare_spoken_words(
            silence_cuts, transcript_words, *_speech_context(source, start, end), duration
        )
        filler_cuts, filler_count = _filler_word_cut_intervals(
            transcript_words, duration, *_speech_context(source, start, end)
        )

    cuts = _merge_cut_intervals([*silence_cuts, *filler_cuts], duration)
    segments = _kept_segments(cuts, duration)
    kept_duration = sum(segment_end - segment_start for segment_start, segment_end in segments)
    # Fail safe: an unusual audio track must never make the editor erase almost
    # the entire video.
    if kept_duration < min(0.5, duration * 0.10):
        cuts = []
        segments = [(0.0, duration)]
    output_duration = _render_kept_segments(source, output, start, segments)
    return {
        "original_duration": round(duration, 3),
        "output_duration": output_duration,
        "removed_seconds": round(max(0.0, duration - output_duration), 3),
        "silence_sections_removed": len(silence_cuts),
        "filler_words_removed": filler_count,
        "cut_count": len(cuts),
    }


def _export_full_length_single_pass(
    source: Path,
    output: Path,
    start: float,
    end: float,
    aspect: Aspect,
    caption_text: str,
    caption_font_scale: float,
    caption_position: CaptionPosition,
    caption_overlay_path: Path | None,
    video_filter: VideoFilter,
    sound_effect: SoundEffect,
    sound_effects: list[SelectedSoundEffect] | tuple[SelectedSoundEffect, ...] | None,
    visual_effect: VisualEffect,
    effect_time: float,
    auto_sound_effect: bool,
    sound_volume: float,
    visual_strength: float,
    live_captions: bool,
    live_caption_scheme: LiveCaptionScheme,
    live_caption_height: float,
    live_caption_scale: float,
    resolution: Resolution,
    title_transcript: bool,
    remove_silence: bool,
    remove_filler_words: bool,
    subscribe_animation: bool,
    export_metadata: dict[str, object] | None,
    remove_still_scenes: bool = False,
) -> dict[SelectedSoundEffect, list[float]]:
    """Analyze once and render a complete landscape or square edit in one generation."""
    if aspect not in {"16:9", "1:1"}:
        raise ValueError("Full-length edits support the 16:9 or 1:1 standard layout.")
    width, height = output_size(aspect, resolution)
    info = probe_video(source)
    frame_rate = info.frame_rate
    selection_start = max(0.0, min(float(start), info.duration))
    selection_end = max(selection_start + 0.1, min(float(end), info.duration))
    selection_duration = max(0.1, selection_end - selection_start)
    filter_chain = _video_filter_chain(video_filter)
    if sound_effect not in {"none", "vine-boom", "check-sound"}:
        raise ValueError("Unknown sound effect.")
    selected_sound_effects = list(dict.fromkeys(sound_effects or ()))
    if sound_effect != "none" and sound_effect not in selected_sound_effects:
        selected_sound_effects.append(sound_effect)
    if any(effect not in {"vine-boom", "check-sound"} for effect in selected_sound_effects):
        raise ValueError("Unknown sound effect.")
    if visual_effect not in {"none", "lens-flare", "punch-zoom", "white-flash"}:
        raise ValueError("Unknown visual effect.")
    if live_caption_scheme not in LIVE_CAPTION_SCHEMES:
        raise ValueError("Unknown live-caption colour scheme.")

    needs_transcript = bool(
        remove_silence
        or remove_filler_words
        or remove_still_scenes
        or live_captions
        or title_transcript
        or (auto_sound_effect and selected_sound_effects)
    )

    def analyze_speech() -> tuple[list[tuple[float, float]], tuple[np.ndarray | None, list | None]]:
        silence = (
            _speech_pause_cut_intervals(source, selection_start, selection_end) if remove_silence else []
        )
        context = (
            _speech_context(source, selection_start, selection_end)
            if remove_silence or remove_filler_words or remove_still_scenes
            else (None, None)
        )
        return silence, context

    # The transcript, the pause analysis, and the picture analysis don't depend on
    # each other, so they run side by side.
    transcript_words: list[CaptionWord] = []
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix=f"{APP_SLUG}-analysis") as pool:
        speech_job = pool.submit(analyze_speech)
        still_job = (
            pool.submit(_still_stretches, source, selection_start, selection_end) if remove_still_scenes else None
        )
        if needs_transcript:
            transcript_job = pool.submit(
                _transcribe_words_cached,
                source, selection_start, selection_end, include_fillers=remove_filler_words,
            )
            try:
                transcript_words = transcript_job.result()
            except (OSError, RuntimeError, ValueError) as exc:
                if remove_filler_words:
                    raise ValueError(f"Filler-word removal needs the bundled offline speech engine: {exc}") from exc
                if live_captions:
                    raise
        silence_cuts, speech_context = speech_job.result()

        still_stretches = still_job.result() if still_job is not None else []

    # Pause removal never cuts a word the speech detector missed but Whisper heard.
    silence_cuts = _spare_spoken_words(silence_cuts, transcript_words, *speech_context, selection_duration)
    filler_cuts: list[tuple[float, float]] = []
    filler_count = 0
    if remove_filler_words:
        filler_cuts, filler_count = _filler_word_cut_intervals(
            transcript_words, selection_duration, *speech_context
        )
    levels, speech_segments = speech_context
    still_cuts = _still_scene_cut_intervals(
        still_stretches, speech_segments, transcript_words, levels, selection_duration
    )
    cuts = _merge_cut_intervals([*silence_cuts, *filler_cuts, *still_cuts], selection_duration)
    segments = _kept_segments(cuts, selection_duration)
    output_duration = sum(segment_end - segment_start for segment_start, segment_end in segments)
    if output_duration < min(0.5, selection_duration * 0.10):
        segments = [(0.0, selection_duration)]
        output_duration = selection_duration
        silence_cuts = []
        still_cuts = []
        filler_count = 0
    # Everything downstream follows the segments actually kept, so a sliver too
    # short to keep counts as removed for captions and sound placement too.
    cuts = _gaps_between(segments, selection_duration)
    output_duration = round(output_duration, 3)
    cleaned_words = _remap_transcript_after_cuts(transcript_words, cuts, selection_duration)

    placements: dict[SelectedSoundEffect, list[float]] = {}
    if selected_sound_effects:
        if auto_sound_effect:
            placements = suggest_sound_effect_placements(
                source,
                selection_start,
                selection_end,
                selected_sound_effects,
                transcript_words,
                effect_time,
                timeline_cuts=cuts,
            )
        else:
            manual_time = round(min(max(0.0, effect_time), max(0.0, output_duration - 0.05)), 2)
            placements = {effect: [manual_time] for effect in selected_sound_effects}

    temporary_paths: list[Path] = []
    live_caption_ass: Path | None = None
    try:
        if live_captions and cleaned_words:
            caption_file = tempfile.NamedTemporaryFile(
                prefix=f"{APP_SLUG}-live-captions-", suffix=".ass", delete=False
            )
            live_caption_ass = Path(caption_file.name)
            caption_file.close()
            temporary_paths.append(live_caption_ass)
            caption_width, caption_height = ASPECT_SIZES[aspect]
            write_live_caption_ass(
                cleaned_words,
                live_caption_ass,
                caption_width,
                caption_height,
                live_caption_scheme,
                live_caption_height,
                live_caption_scale,
            )

        subscribe_asset = EFFECT_ASSETS_DIR / "youtube-subscribe.mov"
        if subscribe_animation and not subscribe_asset.is_file():
            raise RuntimeError("The bundled YouTube subscribe animation is missing.")

        # First the audio: the kept speech with its sound effects and the subscribe
        # chime mixed in, stored losslessly. It takes seconds; the AAC encode then
        # happens inside the video encode, alongside the picture, at no extra time.
        has_audio = _has_audio(source)
        audio_ffmpeg = ffmpeg_executable()
        audio_inputs: list[str] = []
        if has_audio:
            audio_inputs += [
                "-f", "s16le", "-ar", str(_EDIT_SAMPLE_RATE), "-ac", str(_EDIT_CHANNELS), "-i", "pipe:0",
            ]
        next_audio_input = 1 if has_audio else 0
        sound_indexes: dict[SelectedSoundEffect, int] = {}
        for effect in selected_sound_effects:
            if not placements.get(effect):
                continue
            sound_file = tempfile.NamedTemporaryFile(prefix=f"{APP_SLUG}-sfx-", suffix=".wav", delete=False)
            sound_path = Path(sound_file.name)
            sound_file.close()
            temporary_paths.append(sound_path)
            _render_sound_effect(effect, sound_path)
            sound_indexes[effect] = next_audio_input
            audio_inputs += ["-i", str(sound_path)]
            next_audio_input += 1
        subscribe_audio_index: int | None = None
        if subscribe_animation:
            subscribe_audio_index = next_audio_input
            audio_inputs += ["-i", str(subscribe_asset)]
            next_audio_input += 1

        audio_filters: list[str] = []
        volume = min(2.0, max(0.0, float(sound_volume)))
        effect_labels: list[str] = []
        duck_events: list[tuple[SelectedSoundEffect, float]] = []
        for effect_number, (effect, sound_index) in enumerate(sound_indexes.items()):
            effect_times = placements[effect]
            split_labels = [f"sfxsource{effect_number}_{index}" for index in range(len(effect_times))]
            if len(split_labels) > 1:
                audio_filters.append(
                    f"[{sound_index}:a]asplit={len(split_labels)}"
                    + "".join(f"[{label}]" for label in split_labels)
                )
            else:
                split_labels = [f"{sound_index}:a"]
            for index, (split_label, effect_trigger) in enumerate(zip(split_labels, effect_times)):
                delayed_label = f"sfx{effect_number}_{index}"
                audio_filters.append(
                    f"[{split_label}]adelay={round(effect_trigger * 1000)}:all=1,"
                    f"volume={volume:.3f}[{delayed_label}]"
                )
                effect_labels.append(delayed_label)
                duck_events.append((effect, effect_trigger))

        extra_audio_labels = list(effect_labels)
        if subscribe_audio_index is not None:
            audio_filters.append(f"[{subscribe_audio_index}:a]asetpts=PTS-STARTPTS[subscribeaudio]")
            extra_audio_labels.append("subscribeaudio")
        base_audio_label: str | None = "0:a" if has_audio else None
        if base_audio_label is None and extra_audio_labels:
            audio_filters.append(
                f"anullsrc=r={_EDIT_SAMPLE_RATE}:cl=stereo,atrim=0:{output_duration:.3f},"
                "asetpts=PTS-STARTPTS[silentbase]"
            )
            base_audio_label = "silentbase"

        audio_label: str | None = None
        if base_audio_label is not None:
            # The mix is padded to the full length so the audio can never end early.
            audio_filters.append(
                f"[{base_audio_label}]apad=whole_dur={output_duration:.3f},"
                f"atrim=0:{output_duration:.3f}[mixbase]"
            )
            audio_label = "mixbase"
        if audio_label is not None and extra_audio_labels:
            volume_expressions: list[str] = []
            if subscribe_audio_index is not None:
                volume_expressions.append("if(lt(t\\,3.717)\\,0.82\\,1)")
            if effect_labels and volume > 0:
                duck_gain = max(0.30, 1.0 - 0.70 * min(1.0, volume))
                for effect, effect_trigger in duck_events:
                    hold_seconds = 0.68 if effect == "check-sound" else 0.82
                    release_seconds = 0.96 if effect == "check-sound" else 1.10
                    attack_start = max(0.0, effect_trigger - 0.08)
                    attack_end = min(output_duration, max(effect_trigger, attack_start + 0.01))
                    release_start = min(output_duration, max(attack_end, effect_trigger + hold_seconds))
                    release_end = min(output_duration, max(release_start + 0.01, effect_trigger + release_seconds))
                    attack_duration = max(0.01, attack_end - attack_start)
                    release_duration = max(0.01, release_end - release_start)
                    volume_expressions.append(
                        f"if(between(t\\,{attack_start:.3f}\\,{attack_end:.3f})\\,"
                        f"1-(1-{duck_gain:.3f})*(t-{attack_start:.3f})/{attack_duration:.3f}\\,"
                        f"if(between(t\\,{attack_end:.3f}\\,{release_start:.3f})\\,{duck_gain:.3f}\\,"
                        f"if(between(t\\,{release_start:.3f}\\,{release_end:.3f})\\,"
                        f"{duck_gain:.3f}+(1-{duck_gain:.3f})*(t-{release_start:.3f})/{release_duration:.3f}\\,1)))"
                    )
            mixed_base = "mixbase"
            if volume_expressions:
                audio_filters.append(
                    f"[{mixed_base}]volume='{'*'.join(f'({value})' for value in volume_expressions)}':"
                    "eval=frame[duckedbase]"
                )
                mixed_base = "duckedbase"
            mix_inputs = f"[{mixed_base}]" + "".join(f"[{label}]" for label in extra_audio_labels)
            audio_filters.append(
                f"{mix_inputs}amix=inputs={len(extra_audio_labels) + 1}:"
                "duration=first:dropout_transition=0:normalize=0,"
                "alimiter=limit=0.95[outa]"
            )
            audio_label = "outa"

        audio_track: Path | None = None
        if audio_label is not None:
            audio_file = tempfile.NamedTemporaryFile(prefix=f"{APP_SLUG}-audio-", suffix=".flac", delete=False)
            audio_track = Path(audio_file.name)
            audio_file.close()
            temporary_paths.append(audio_track)
            audio_command = [
                audio_ffmpeg, "-y", "-v", "error", *audio_inputs,
                *_filter_graph_args(audio_ffmpeg, ";".join(audio_filters), temporary_paths),
                "-map", f"[{audio_label}]", "-t", f"{output_duration:.3f}", *_LOSSLESS_AUDIO,
                str(audio_track),
            ]
            if has_audio:
                _run_with_kept_audio(audio_command, source, selection_start, selection_duration, segments)
            else:
                _run(audio_command)

        # Then the picture, in one encode.
        ffmpeg = ffmpeg_executable(require_ass=live_caption_ass is not None)
        inputs = [
            ffmpeg, "-y", "-v", "error", *_STEADY_INPUT,
            "-ss", f"{selection_start:.3f}", "-t", f"{selection_duration:.3f}",
            "-i", str(source),
        ]
        next_input = 1

        overlay_index: int | None = None
        if visual_effect in {"lens-flare", "white-flash"}:
            overlay_file = tempfile.NamedTemporaryFile(prefix=f"{APP_SLUG}-vfx-", suffix=".png", delete=False)
            overlay_path = Path(overlay_file.name)
            overlay_file.close()
            temporary_paths.append(overlay_path)
            _render_visual_overlay(visual_effect, overlay_path, width, height, visual_strength)
            overlay_index = next_input
            inputs += ["-loop", "1", "-i", str(overlay_path)]
            next_input += 1

        subscribe_index: int | None = None
        if subscribe_animation:
            subscribe_index = next_input
            inputs += ["-i", str(subscribe_asset)]
            next_input += 1

        creator_caption_index: int | None = None
        if aspect == "1:1" and (caption_text.strip() or caption_overlay_path is not None):
            owns_caption_overlay = caption_overlay_path is None
            if owns_caption_overlay:
                caption_file = tempfile.NamedTemporaryFile(
                    prefix=f"{APP_SLUG}-caption-", suffix=".png", delete=False
                )
                rendered_caption_overlay = Path(caption_file.name)
                caption_file.close()
                _render_square_caption(
                    caption_text,
                    rendered_caption_overlay,
                    caption_font_scale,
                    caption_position,
                )
                temporary_paths.append(rendered_caption_overlay)
            else:
                rendered_caption_overlay = caption_overlay_path
            creator_caption_index = next_input
            inputs += ["-loop", "1", "-i", str(rendered_caption_overlay)]
            next_input += 1

        audio_index: int | None = None
        if audio_track is not None:
            audio_index = next_input
            inputs += ["-i", str(audio_track)]
            next_input += 1

        base_video_filter = (
            f"[0:v]{_kept_frames_filter(segments, selection_duration)},{_fit_frame(width, height)}"
        )
        if filter_chain:
            base_video_filter += f",{filter_chain}"
        filters = [f"{base_video_filter}[basev]"]
        video_label = "basev"
        trigger = min(max(0.0, float(effect_time)), max(0.0, output_duration - 0.05))
        strength = min(1.5, max(0.25, float(visual_strength)))
        if visual_effect == "punch-zoom":
            zoom_duration = min(0.70, max(0.25, output_duration - trigger))
            amount = 0.10 * strength
            zoom_end = trigger + zoom_duration
            scale = (
                f"if(between(t\\,{trigger:.3f}\\,{zoom_end:.3f})\\,"
                f"1+{amount:.4f}*sin(PI*(t-{trigger:.3f})/{zoom_duration:.3f})\\,1)"
            )
            filters.append(
                f"[{video_label}]scale=w='iw*{scale}':h='ih*{scale}':eval=frame,"
                f"crop={width}:{height}:(in_w-out_w)/2:(in_h-out_h)/2[vfx]"
            )
            video_label = "vfx"
        elif overlay_index is not None:
            if visual_effect == "lens-flare":
                fade_out_start, fade_out_duration = trigger + 0.18, 0.58
            else:
                fade_out_start, fade_out_duration = trigger + 0.07, 0.24
            filters.append(
                f"[{overlay_index}:v]format=rgba,"
                f"fade=t=in:st={trigger:.3f}:d=0.08:alpha=1,"
                f"fade=t=out:st={fade_out_start:.3f}:d={fade_out_duration:.3f}:alpha=1[overlayfx]"
            )
            filters.append(f"[{video_label}][overlayfx]overlay=0:0:eof_action=pass[vfx]")
            video_label = "vfx"

        if live_caption_ass is not None:
            caption_path = str(live_caption_ass).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
            filters.append(f"[{video_label}]ass=filename='{caption_path}'[captioned]")
            video_label = "captioned"
        if subscribe_index is not None:
            filters.append(
                f"[{subscribe_index}:v]setpts=PTS-STARTPTS,"
                f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags=lanczos,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black@0,format=rgba[subscribe]"
            )
            filters.append(
                f"[{video_label}][subscribe]overlay=0:(H-h)/2:eof_action=pass:"
                "shortest=0:format=auto[withsubscribe]"
            )
            video_label = "withsubscribe"
        if creator_caption_index is not None:
            filters.append(
                f"[{creator_caption_index}:v]scale={width}:{height}:flags=lanczos,format=rgba[creatorcaption]"
            )
            filters.append(
                f"[{video_label}][creatorcaption]overlay=0:0:eof_action=pass[withcreatorcaption]"
            )
            video_label = "withcreatorcaption"

        graph = _filter_graph_args(ffmpeg, ";".join(filters), temporary_paths)

        def command_for(codec: list[str]) -> list[str]:
            command = [*inputs, *graph, "-map", f"[{video_label}]"]
            if audio_index is not None:
                command += ["-map", f"{audio_index}:a", "-c:a", "aac", "-b:a", "192k"]
            return command + [
                "-t", f"{output_duration:.3f}", *codec, *_frame_rate_args(frame_rate),
                "-movflags", "+faststart", str(output),
            ]

        video_encoder = _encode_video(command_for, width, height, frame_rate, crf=18)

        if export_metadata is not None:
            export_metadata["width"] = width
            export_metadata["height"] = height
            export_metadata["resolution"] = resolution
            export_metadata["frame_rate"] = format_frame_rate(frame_rate)
            export_metadata["video_encoder"] = video_encoder
            export_metadata["live_caption_word_count"] = len(cleaned_words) if live_captions else 0
            export_metadata["title_transcript"] = " ".join(word.text for word in cleaned_words)[:2_000]
            export_metadata["full_length_summary"] = {
                "original_duration": round(selection_duration, 3),
                "output_duration": output_duration,
                "removed_seconds": round(max(0.0, selection_duration - output_duration), 3),
                "silence_sections_removed": len(silence_cuts),
                "filler_words_removed": filler_count,
                "still_scenes_removed": len(still_cuts),
                "still_seconds_removed": round(sum(end - start for start, end in still_cuts), 3),
                "cut_count": len(cuts),
                "remove_silence": bool(remove_silence),
                "remove_filler_words": bool(remove_filler_words),
                "remove_still_scenes": bool(remove_still_scenes),
                "subscribe_animation": bool(subscribe_animation),
                "render_passes": 1,
                "shared_transcript": bool(needs_transcript),
                "quality": "single-pass-crf18",
                "video_encoder": video_encoder,
                "aspect": aspect,
                "width": width,
                "height": height,
                "square_caption": bool(creator_caption_index is not None),
                "live_caption_margin": live_caption_margin(*ASPECT_SIZES[aspect], live_caption_height),
                "live_caption_font_size": live_caption_font_size(*ASPECT_SIZES[aspect], live_caption_scale),
            }
        return placements
    finally:
        for temporary_path in temporary_paths:
            temporary_path.unlink(missing_ok=True)


def _apply_subscribe_animation(source: Path, output: Path) -> None:
    """Overlay the complete creator-supplied YouTube animation from frame zero."""
    asset = EFFECT_ASSETS_DIR / "youtube-subscribe.mov"
    if not asset.is_file():
        raise RuntimeError("The bundled YouTube subscribe animation is missing.")
    info = probe_video(source)
    duration = max(0.1, info.duration)
    filters = [
        f"[1:v]setpts=PTS-STARTPTS,scale={info.width}:-2:flags=lanczos,format=rgba[subscribe]",
        "[0:v][subscribe]overlay=0:(H-h)/2:eof_action=pass:shortest=0:format=auto[outv]",
    ]
    audio_label: str | None = None
    if _has_audio(source):
        filters += [
            "[0:a]asetpts=PTS-STARTPTS,volume='if(lt(t,3.717),0.82,1)':eval=frame[baseaudio]",
            "[1:a]asetpts=PTS-STARTPTS[subscribeaudio]",
            "[baseaudio][subscribeaudio]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
            "alimiter=limit=0.95[outa]",
        ]
        audio_label = "outa"
    else:
        filters.append(f"[1:a]asetpts=PTS-STARTPTS,apad,atrim=0:{duration:.3f}[outa]")
        audio_label = "outa"

    def command_for(codec: list[str]) -> list[str]:
        return [
            ffmpeg_executable(), "-y", "-v", "error", "-i", str(source), "-i", str(asset),
            "-filter_complex", ";".join(filters), "-map", "[outv]", "-map", f"[{audio_label}]",
            "-t", f"{duration:.3f}", *codec, "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
            str(output),
        ]

    _encode_video(command_for, info.width - info.width % 2, info.height - info.height % 2, info.frame_rate, crf=19)


def export_clip(
    source: Path,
    output: Path,
    start: float,
    end: float,
    aspect: Aspect,
    layout: Literal["standard", "gaming"] = "standard",
    face_corner: FaceCorner = "top-right",
    face_width_fraction: float = 0.30,
    face_height_fraction: float = 0.34,
    face_inset_x_fraction: float = 0.02,
    face_inset_y_fraction: float = 0.02,
    caption_text: str = "",
    caption_font_scale: float = 1.0,
    caption_position: CaptionPosition = "center",
    caption_overlay_path: Path | None = None,
    video_filter: VideoFilter = "none",
    sound_effect: SoundEffect = "none",
    sound_effects: list[SelectedSoundEffect] | tuple[SelectedSoundEffect, ...] | None = None,
    visual_effect: VisualEffect = "none",
    effect_time: float = 1.0,
    auto_sound_effect: bool = True,
    sound_volume: float = 1.0,
    visual_strength: float = 1.0,
    live_captions: bool = False,
    live_caption_scheme: LiveCaptionScheme = "pilot-lime",
    live_caption_height: float = DEFAULT_LIVE_CAPTION_HEIGHT,
    live_caption_scale: float = DEFAULT_LIVE_CAPTION_SCALE,
    resolution: Resolution = DEFAULT_RESOLUTION,
    title_transcript: bool = False,
    export_metadata: dict[str, object] | None = None,
    edit_mode: Literal["clip", "full-length"] = "clip",
    remove_silence: bool = False,
    remove_filler_words: bool = False,
    subscribe_animation: bool = False,
    remove_still_scenes: bool = False,
) -> dict[SelectedSoundEffect, list[float]]:
    if edit_mode not in {"clip", "full-length"}:
        raise ValueError("Unknown edit mode.")
    if edit_mode == "full-length":
        if aspect not in {"16:9", "1:1"} or layout != "standard":
            raise ValueError("Full-length edits use the 16:9 or 1:1 standard layout.")
        return _export_full_length_single_pass(
            source=source,
            output=output,
            start=start,
            end=end,
            aspect=aspect,
            caption_text=caption_text,
            caption_font_scale=caption_font_scale,
            caption_position=caption_position,
            caption_overlay_path=caption_overlay_path,
            video_filter=video_filter,
            sound_effect=sound_effect,
            sound_effects=sound_effects,
            visual_effect=visual_effect,
            effect_time=effect_time,
            auto_sound_effect=auto_sound_effect,
            sound_volume=sound_volume,
            visual_strength=visual_strength,
            live_captions=live_captions,
            live_caption_scheme=live_caption_scheme,
            live_caption_height=live_caption_height,
            live_caption_scale=live_caption_scale,
            resolution=resolution,
            title_transcript=title_transcript,
            remove_silence=remove_silence,
            remove_filler_words=remove_filler_words,
            subscribe_animation=subscribe_animation,
            export_metadata=export_metadata,
            remove_still_scenes=remove_still_scenes,
        )

    info = probe_video(source)
    start = max(0.0, min(start, info.duration))
    end = max(start + 0.1, min(end, info.duration))
    duration = end - start
    filter_chain = _video_filter_chain(video_filter)
    width, height = output_size(aspect, resolution)
    frame_rate = info.frame_rate
    video_encoders: list[str] = []

    def encode(command_for: Callable[[list[str]], list[str]]) -> None:
        video_encoders.append(_encode_video(
            lambda codec: command_for([*codec, *_frame_rate_args(frame_rate)]),
            width, height, frame_rate, crf=20,
        ))

    if sound_effect not in {"none", "vine-boom", "check-sound"}:
        raise ValueError("Unknown sound effect.")
    selected_sound_effects = list(dict.fromkeys(sound_effects or ()))
    if sound_effect != "none" and sound_effect not in selected_sound_effects:
        selected_sound_effects.append(sound_effect)
    if any(effect not in {"vine-boom", "check-sound"} for effect in selected_sound_effects):
        raise ValueError("Unknown sound effect.")
    if visual_effect not in {"none", "lens-flare", "punch-zoom", "white-flash"}:
        raise ValueError("Unknown visual effect.")
    if live_caption_scheme not in LIVE_CAPTION_SCHEMES:
        raise ValueError("Unknown live-caption colour scheme.")

    live_caption_ass: Path | None = None
    live_caption_word_count = 0
    transcript_words = []
    if live_captions or title_transcript or (auto_sound_effect and selected_sound_effects):
        try:
            transcript_words = _transcribe_words_cached(source, start, end)
        except (OSError, RuntimeError, ValueError):
            if live_captions:
                raise
            transcript_words = []
    sound_effect_placements: dict[SelectedSoundEffect, list[float]] = {}
    if selected_sound_effects:
        if auto_sound_effect:
            sound_effect_placements = suggest_sound_effect_placements(
                source,
                start,
                end,
                selected_sound_effects,
                transcript_words,
                effect_time,
            )
        else:
            manual_time = round(min(max(0.0, effect_time), max(0.0, duration - 0.05)), 2)
            sound_effect_placements = {effect: [manual_time] for effect in selected_sound_effects}
    if live_captions:
        live_caption_word_count = len(transcript_words)
        words = transcript_words
        if words:
            caption_file = tempfile.NamedTemporaryFile(
                prefix=f"{APP_SLUG}-live-captions-", suffix=".ass", delete=False
            )
            live_caption_ass = Path(caption_file.name)
            caption_file.close()
            # Captions are laid out on the 1080p reference frame; libass scales
            # the script to the export's real size, so every resolution matches.
            caption_width, caption_height = ASPECT_SIZES[aspect]
            write_live_caption_ass(
                words,
                live_caption_ass,
                caption_width,
                caption_height,
                live_caption_scheme,
                live_caption_height,
                live_caption_scale,
            )
    if export_metadata is not None:
        export_metadata["width"] = width
        export_metadata["height"] = height
        export_metadata["resolution"] = resolution
        export_metadata["frame_rate"] = format_frame_rate(frame_rate)
        export_metadata["video_encoder"] = video_encoders[0] if video_encoders else ""
        export_metadata["live_caption_word_count"] = live_caption_word_count
        export_metadata["live_caption_margin"] = live_caption_margin(*ASPECT_SIZES[aspect], live_caption_height)
        export_metadata["live_caption_font_size"] = live_caption_font_size(*ASPECT_SIZES[aspect], live_caption_scale)
        export_metadata["title_transcript"] = " ".join(word.text for word in transcript_words)[:2_000]

    has_effects = bool(selected_sound_effects) or visual_effect != "none"
    has_postprocessing = has_effects or live_caption_ass is not None
    base_temporary: Path | None = None
    render_target = output
    if has_postprocessing:
        base_file = tempfile.NamedTemporaryFile(prefix=f"{APP_SLUG}-base-", suffix=".mp4", delete=False)
        base_temporary = Path(base_file.name)
        base_file.close()
        render_target = base_temporary

    common = [
        ffmpeg_executable(), "-y", "-v", "error",
        "-ss", f"{start:.3f}", "-i", str(source),
        "-t", f"{duration:.3f}",
    ]
    # The sound comes from the repaired audio when the video's audio needed one.
    # It is added before "-t", which must stay the output's own option.
    clip_audio = audio_source(source)
    repaired_audio = ["-ss", f"{start:.3f}", "-i", str(clip_audio)] if clip_audio != source else []

    def audio_map(index: int) -> str:
        return f"{index}:a" if repaired_audio else "0:a?"

    try:
        if layout == "gaming":
            if aspect != "9:16":
                raise ValueError("Gaming face-cam layout is currently designed for 9:16 exports.")

            fw, fh, fx, fy = _face_crop(
                info,
                face_corner,
                face_width_fraction,
                face_height_fraction,
                face_inset_x_fraction,
                face_inset_y_fraction,
            )
            # 36% of the vertical frame is face-cam, 64% gameplay (690 of 1920 at 1080p).
            face_h = 2 * round(345 * height / 1920)
            game_h = height - face_h
            source_filters = f"{filter_chain}," if filter_chain else ""
            # Stacking both crops of the source keeps its frame rate. A generated
            # background would impose its own rate (25 fps) on the whole clip.
            filter_complex = (
                f"[0:v]{source_filters}split=2[face][game];"
                f"[face]crop={fw}:{fh}:{fx}:{fy},"
                f"scale={width}:{face_h}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={width}:{face_h},setsar=1[faceout];"
                f"[game]scale={width}:{game_h}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={width}:{game_h},setsar=1[gameout];"
                f"[faceout][gameout]vstack=inputs=2[outv]"
            )
            encode(lambda codec: common[:-2] + repaired_audio + common[-2:] + [
                "-filter_complex", filter_complex,
                "-map", "[outv]", "-map", audio_map(1),
                *codec,
                "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart",
                str(render_target),
            ])
        elif aspect == "1:1" and (caption_text.strip() or caption_overlay_path is not None):
            owns_overlay = caption_overlay_path is None
            if owns_overlay:
                overlay_file = tempfile.NamedTemporaryFile(prefix=f"{APP_SLUG}-caption-", suffix=".png", delete=False)
                overlay_path = Path(overlay_file.name)
                overlay_file.close()
            else:
                overlay_path = caption_overlay_path
            try:
                if owns_overlay:
                    _render_square_caption(caption_text, overlay_path, caption_font_scale, caption_position)
                base_filters = (
                    f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
                    f"crop={width}:{height}"
                )
                if filter_chain:
                    base_filters += f",{filter_chain}"
                filter_complex = (
                    f"[0:v]{base_filters}[base];"
                    f"[1:v]scale={width}:{height}:flags=lanczos,format=rgba[caption];"
                    f"[base][caption]overlay=0:0:eof_action=repeat[outv]"
                )
                encode(lambda codec: common[:-2] + ["-i", str(overlay_path)] + repaired_audio + common[-2:] + [
                    "-filter_complex", filter_complex,
                    "-map", "[outv]", "-map", audio_map(2),
                    *codec,
                    "-c:a", "aac", "-b:a", "160k",
                    "-movflags", "+faststart",
                    str(render_target),
                ])
            finally:
                if owns_overlay:
                    overlay_path.unlink(missing_ok=True)
        else:
            vf = (
                f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={width}:{height}"
            )
            if filter_chain:
                vf += f",{filter_chain}"
            encode(lambda codec: common[:-2] + repaired_audio + common[-2:] + [
                "-vf", vf,
                "-map", "0:v:0", "-map", audio_map(1),
                *codec,
                "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart",
                str(render_target),
            ])

        if has_postprocessing:
            _apply_effects(
                render_target,
                output,
                duration,
                width,
                height,
                selected_sound_effects[0] if selected_sound_effects else "none",
                visual_effect,
                effect_time,
                None,
                sound_volume,
                visual_strength,
                live_caption_ass,
                sound_effect_placements,
                frame_rate,
            )
    finally:
        if base_temporary is not None:
            base_temporary.unlink(missing_ok=True)
        if live_caption_ass is not None:
            live_caption_ass.unlink(missing_ok=True)
    return sound_effect_placements
