from __future__ import annotations

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
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .brand import APP_SLUG, env
from .captions import LIVE_CAPTION_SCHEMES, LiveCaptionScheme, transcribe_words, write_live_caption_ass

Aspect = Literal["16:9", "9:16", "1:1"]
FaceCorner = Literal["top-left", "top-right", "bottom-left", "bottom-right"]
CaptionPosition = Literal["top", "center", "bottom"]
SoundEffect = Literal["none", "impact-boom", "vine-boom", "whoosh", "record-scratch"]
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
            "-show_entries", "stream=width,height:format=duration",
            "-of", "json",
            str(path),
        ])
        data = json.loads(result.stdout)
        stream = data["streams"][0]
        duration = float(data.get("format", {}).get("duration", 0) or 0)
        return VideoInfo(width=int(stream["width"]), height=int(stream["height"]), duration=duration)

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
    return VideoInfo(width=int(width), height=int(height), duration=duration)


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
        ffmpeg_executable(), "-v", "error", "-i", str(resolved),
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
    """Render a bundled sound sample or one of the original synthesized effects."""
    if effect == "vine-boom":
        source = EFFECT_ASSETS_DIR / "vine-boom.wav"
        if not source.is_file():
            raise RuntimeError("The bundled Vine Boom sound asset is missing.")
        shutil.copyfile(source, destination)
        return

    sample_rate = 48_000
    lengths = {"impact-boom": 1.25, "whoosh": 0.85, "record-scratch": 0.72}
    duration = lengths.get(effect, 1.0)
    time_axis = np.arange(round(sample_rate * duration), dtype=np.float64) / sample_rate
    rng = np.random.default_rng(0xC11F)

    if effect == "impact-boom":
        sweep_phase = 2 * np.pi * (92 * time_axis - 27 * time_axis * time_axis)
        sub_phase = 2 * np.pi * 43 * time_axis
        low_boom = np.sin(sweep_phase) * np.exp(-3.3 * time_axis)
        sub = 0.48 * np.sin(sub_phase) * np.exp(-4.8 * time_axis)
        transient = rng.normal(0, 1, time_axis.size) * np.exp(-34 * time_axis) * 0.34
        samples = low_boom + sub + transient
    elif effect == "whoosh":
        noise = rng.normal(0, 1, time_axis.size)
        smoothed = np.convolve(noise, np.ones(45) / 45, mode="same")
        envelope = np.sin(np.pi * np.clip(time_axis / duration, 0, 1)) ** 2
        rising_tone = np.sin(2 * np.pi * (170 * time_axis + 680 * time_axis * time_axis))
        samples = (smoothed * 3.1 + rising_tone * 0.16) * envelope
    elif effect == "record-scratch":
        chirp_phase = 2 * np.pi * (1_050 * time_axis - 690 * time_axis * time_axis)
        gate = (np.sin(2 * np.pi * 24 * time_axis) > -0.35).astype(np.float64)
        noise = rng.normal(0, 0.18, time_axis.size)
        samples = (np.sin(chirp_phase) * 0.72 + noise) * gate * np.exp(-1.9 * time_axis)
    else:
        samples = np.zeros_like(time_axis)

    peak = float(np.max(np.abs(samples), initial=1.0))
    pcm = np.int16(np.clip(samples / max(peak, 1e-6) * 0.88, -1, 1) * 32767)
    with wave.open(str(destination), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm.tobytes())


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


def _video_filter_chain(video_filter: VideoFilter) -> str:
    try:
        return VIDEO_FILTER_CHAINS[video_filter]
    except KeyError as exc:
        raise ValueError("Unknown video filter.") from exc


def _clip_audio_envelope(
    source: Path,
    start: float,
    end: float,
    sample_rate: int = 8_000,
    hop_seconds: float = 0.10,
) -> tuple[np.ndarray, float]:
    """Decode a short clip into a fine-grained loudness envelope."""
    duration = max(0.1, float(end) - float(start))
    result = _run([
        ffmpeg_executable(), "-v", "error",
        "-ss", f"{max(0.0, float(start)):.3f}", "-i", str(source),
        "-t", f"{duration:.3f}", "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-f", "s16le", "pipe:1",
    ])
    samples = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    frame_size = max(1, round(sample_rate * hop_seconds))
    if samples.size == 0:
        return np.zeros(max(1, math.ceil(duration / hop_seconds)), dtype=np.float32), hop_seconds
    frame_count = math.ceil(samples.size / frame_size)
    padded = np.pad(samples, (0, frame_count * frame_size - samples.size))
    frames = padded.reshape(frame_count, frame_size)
    envelope = np.sqrt(np.mean(np.square(frames), axis=1)).astype(np.float32)
    return envelope, frame_size / sample_rate


def _clip_scene_change_times(source: Path, start: float, end: float) -> list[float]:
    """Return hard-cut timestamps relative to a short selected clip."""
    duration = max(0.1, float(end) - float(start))
    result = subprocess.run(
        [
            ffmpeg_executable(), "-v", "info",
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
    return [
        value
        for value in (float(match) for match in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", output))
        if 0.35 <= value <= duration - 0.20
    ]


def _smart_sound_times_from_signals(
    envelope: np.ndarray,
    hop_seconds: float,
    duration: float,
    sound_effect: SoundEffect,
    scene_times: list[float] | tuple[float, ...] = (),
    fallback_time: float = 1.0,
) -> list[float]:
    """Rank likely punchline endings, reactions, and cuts for one selected sound.

    This deliberately relies on local audio/visual structure rather than claiming
    to understand the words being spoken. Different effects favor different
    event shapes, and spacing/quality gates prevent repetitive over-editing.
    """
    clip_duration = max(0.1, float(duration))
    if sound_effect == "none":
        return []

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

    weights = {
        "vine-boom": {"phrase-end": 1.30, "reaction": 0.72, "peak": 0.40, "scene": 0.24},
        "impact-boom": {"phrase-end": 0.65, "reaction": 1.18, "peak": 0.92, "scene": 0.76},
        "whoosh": {"phrase-end": 0.22, "reaction": 0.42, "peak": 0.35, "scene": 1.85},
        "record-scratch": {"phrase-end": 1.35, "reaction": 0.42, "peak": 0.24, "scene": 0.58},
    }[sound_effect]
    spacing = {
        "vine-boom": 3.2,
        "impact-boom": 3.6,
        "whoosh": 2.8,
        "record-scratch": 4.2,
    }[sound_effect]
    max_hits = min(6, max(1, int(clip_duration // 9) + 1))
    ranked = sorted(
        (
            (score * weights[kind], min(max(0.35, time_value), max(0.35, clip_duration - 0.20)))
            for time_value, score, kind in candidates
            if 0.30 <= time_value <= clip_duration - 0.15
        ),
        reverse=True,
    )
    selected: list[float] = []
    best_score = ranked[0][0] if ranked else 0.0
    quality_floor = max(0.34, best_score * 0.48)
    for score, time_value in ranked:
        if score < quality_floor or any(abs(time_value - existing) < spacing for existing in selected):
            continue
        selected.append(time_value)
        if len(selected) >= max_hits:
            break

    if not selected:
        fallback = min(max(0.35, float(fallback_time)), max(0.35, clip_duration - 0.20))
        selected = [fallback]
    return [round(value, 2) for value in sorted(selected)]


def suggest_sound_effect_times(
    source: Path,
    start: float,
    end: float,
    sound_effect: SoundEffect,
    fallback_time: float = 1.0,
) -> list[float]:
    """Analyze a selected clip and return smart, repeat-capable SFX timestamps."""
    duration = max(0.1, float(end) - float(start))
    try:
        envelope, hop_seconds = _clip_audio_envelope(source, start, end)
    except (OSError, subprocess.SubprocessError, RuntimeError):
        envelope, hop_seconds = np.zeros(1, dtype=np.float32), 0.10
    try:
        scenes = _clip_scene_change_times(source, start, end)
    except (OSError, subprocess.SubprocessError, RuntimeError):
        scenes = []
    return _smart_sound_times_from_signals(
        envelope,
        hop_seconds,
        duration,
        sound_effect,
        scenes,
        fallback_time,
    )


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
) -> None:
    trigger = min(max(0.0, float(effect_time)), max(0.0, duration - 0.05))
    volume = min(2.0, max(0.0, float(sound_volume)))
    strength = min(1.5, max(0.25, float(visual_strength)))
    temporary_paths: list[Path] = []
    inputs = [ffmpeg_executable(require_ass=live_caption_ass is not None), "-y", "-v", "error", "-i", str(source)]
    sound_index: int | None = None
    overlay_index: int | None = None
    sound_triggers = []
    if sound_effect != "none":
        supplied = [trigger] if sound_effect_times is None else sound_effect_times
        sound_triggers = sorted({
            round(min(max(0.0, float(value)), max(0.0, duration - 0.05)), 3)
            for value in supplied
        })

    try:
        if sound_triggers:
            sound_file = tempfile.NamedTemporaryFile(prefix=f"{APP_SLUG}-sfx-", suffix=".wav", delete=False)
            sound_path = Path(sound_file.name)
            sound_file.close()
            temporary_paths.append(sound_path)
            _render_sound_effect(sound_effect, sound_path)
            sound_index = 1
            inputs += ["-i", str(sound_path)]

        if visual_effect in {"lens-flare", "white-flash"}:
            overlay_file = tempfile.NamedTemporaryFile(prefix=f"{APP_SLUG}-vfx-", suffix=".png", delete=False)
            overlay_path = Path(overlay_file.name)
            overlay_file.close()
            temporary_paths.append(overlay_path)
            _render_visual_overlay(visual_effect, overlay_path, width, height, strength)
            overlay_index = 1 + (1 if sound_index is not None else 0)
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
        if sound_index is not None:
            source_labels = [f"sfxsource{index}" for index in range(len(sound_triggers))]
            if len(source_labels) > 1:
                filters.append(
                    f"[{sound_index}:a]asplit={len(source_labels)}"
                    + "".join(f"[{label}]" for label in source_labels)
                )
            else:
                source_labels = [f"{sound_index}:a"]
            effect_labels: list[str] = []
            for index, (source_label, sound_trigger) in enumerate(zip(source_labels, sound_triggers)):
                delay_ms = round(sound_trigger * 1000)
                effect_label = f"sfx{index}"
                filters.append(
                    f"[{source_label}]adelay={delay_ms}:all=1,volume={volume:.3f}[{effect_label}]"
                )
                effect_labels.append(effect_label)
            effect_inputs = "".join(f"[{label}]" for label in effect_labels)
            if _has_audio(source):
                filters.append(
                    f"[0:a]{effect_inputs}amix=inputs={len(effect_labels) + 1}:"
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

        command = inputs
        if filters:
            command += ["-filter_complex", ";".join(filters)]
        command += ["-map", f"[{video_label}]" if video_label != "0:v" else "0:v:0"]
        if audio_label:
            command += ["-map", f"[{audio_label}]"]
        else:
            command += ["-map", "0:a?"]
        command += [
            "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", str(output),
        ]
        _run(command)
    finally:
        for temporary in temporary_paths:
            temporary.unlink(missing_ok=True)


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
    visual_effect: VisualEffect = "none",
    effect_time: float = 1.0,
    auto_sound_effect: bool = True,
    sound_volume: float = 1.0,
    visual_strength: float = 1.0,
    live_captions: bool = False,
    live_caption_scheme: LiveCaptionScheme = "pilot-lime",
    title_transcript: bool = False,
    export_metadata: dict[str, object] | None = None,
) -> list[float]:
    info = probe_video(source)
    start = max(0.0, min(start, info.duration))
    end = max(start + 0.1, min(end, info.duration))
    duration = end - start
    filter_chain = _video_filter_chain(video_filter)
    if sound_effect not in {"none", "impact-boom", "vine-boom", "whoosh", "record-scratch"}:
        raise ValueError("Unknown sound effect.")
    if visual_effect not in {"none", "lens-flare", "punch-zoom", "white-flash"}:
        raise ValueError("Unknown visual effect.")
    if live_caption_scheme not in LIVE_CAPTION_SCHEMES:
        raise ValueError("Unknown live-caption colour scheme.")

    sound_effect_times: list[float] = []
    if sound_effect != "none":
        if auto_sound_effect:
            sound_effect_times = suggest_sound_effect_times(source, start, end, sound_effect, effect_time)
        else:
            sound_effect_times = [round(min(max(0.0, effect_time), max(0.0, duration - 0.05)), 2)]

    live_caption_ass: Path | None = None
    live_caption_word_count = 0
    transcript_words = []
    if live_captions or title_transcript:
        try:
            transcript_words = transcribe_words(source, start, end, ffmpeg_executable())
        except (OSError, RuntimeError, ValueError):
            if live_captions:
                raise
            transcript_words = []
    if live_captions:
        live_caption_word_count = len(transcript_words)
        words = transcript_words
        if words:
            caption_file = tempfile.NamedTemporaryFile(
                prefix=f"{APP_SLUG}-live-captions-", suffix=".ass", delete=False
            )
            live_caption_ass = Path(caption_file.name)
            caption_file.close()
            width, height = ASPECT_SIZES[aspect]
            write_live_caption_ass(words, live_caption_ass, width, height, live_caption_scheme)
    if export_metadata is not None:
        export_metadata["live_caption_word_count"] = live_caption_word_count
        export_metadata["title_transcript"] = " ".join(word.text for word in transcript_words)[:2_000]

    has_effects = sound_effect != "none" or visual_effect != "none"
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
            # 36% of the vertical frame is face-cam, 64% gameplay.
            face_h = 690
            game_h = 1230
            source_filters = f"{filter_chain}," if filter_chain else ""
            filter_complex = (
                f"[0:v]{source_filters}split=2[face][game];"
                f"[face]crop={fw}:{fh}:{fx}:{fy},"
                f"scale=1080:{face_h}:force_original_aspect_ratio=increase,crop=1080:{face_h}[faceout];"
                f"[game]scale=1080:{game_h}:force_original_aspect_ratio=increase,crop=1080:{game_h}[gameout];"
                f"color=c=black:s=1080x1920:d={duration:.3f}[bg];"
                f"[bg][faceout]overlay=0:0[tmp];"
                f"[tmp][gameout]overlay=0:{face_h}[outv]"
            )
            cmd = common + [
                "-filter_complex", filter_complex,
                "-map", "[outv]", "-map", "0:a?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart",
                str(render_target),
            ]
            _run(cmd)
        elif aspect == "1:1" and (caption_text.strip() or caption_overlay_path is not None):
            width, height = ASPECT_SIZES[aspect]
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
                    f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                    f"crop={width}:{height}"
                )
                if filter_chain:
                    base_filters += f",{filter_chain}"
                filter_complex = (
                    f"[0:v]{base_filters}[base];"
                    f"[base][1:v]overlay=0:0:eof_action=repeat[outv]"
                )
                cmd = common[:-2] + ["-i", str(overlay_path)] + common[-2:] + [
                    "-filter_complex", filter_complex,
                    "-map", "[outv]", "-map", "0:a?",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-c:a", "aac", "-b:a", "160k",
                    "-movflags", "+faststart",
                    str(render_target),
                ]
                _run(cmd)
            finally:
                if owns_overlay:
                    overlay_path.unlink(missing_ok=True)
        else:
            width, height = ASPECT_SIZES[aspect]
            vf = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
            if filter_chain:
                vf += f",{filter_chain}"
            cmd = common + [
                "-vf", vf,
                "-map", "0:v:0", "-map", "0:a?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart",
                str(render_target),
            ]
            _run(cmd)

        if has_postprocessing:
            width, height = ASPECT_SIZES[aspect]
            _apply_effects(
                render_target,
                output,
                duration,
                width,
                height,
                sound_effect,
                visual_effect,
                effect_time,
                sound_effect_times,
                sound_volume,
                visual_strength,
                live_caption_ass,
            )
    finally:
        if base_temporary is not None:
            base_temporary.unlink(missing_ok=True)
        if live_caption_ass is not None:
            live_caption_ass.unlink(missing_ok=True)
    return sound_effect_times
