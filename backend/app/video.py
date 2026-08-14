from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
from PIL import Image, ImageDraw, ImageFont

Aspect = Literal["16:9", "9:16", "1:1"]
FaceCorner = Literal["top-left", "top-right", "bottom-left", "bottom-right"]
SoundEffect = Literal["none", "impact-boom", "whoosh", "record-scratch"]
VisualEffect = Literal["none", "lens-flare", "punch-zoom", "white-flash"]

ASPECT_SIZES: dict[Aspect, tuple[int, int]] = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
}


@dataclass
class VideoInfo:
    width: int
    height: int
    duration: float


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def ffmpeg_executable() -> str:
    configured = os.environ.get("CLIPPILOT_FFMPEG_EXE")
    if configured and Path(configured).exists():
        return configured

    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg

    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:
        raise RuntimeError("FFmpeg is not available.") from exc


def probe_video(path: Path) -> VideoInfo:
    configured_probe = os.environ.get("CLIPPILOT_FFPROBE_EXE")
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


def _audio_rms_per_second(path: Path, sample_rate: int = 16000) -> np.ndarray:
    # Decode audio directly into memory as mono signed 16-bit PCM.
    process = subprocess.run([
        ffmpeg_executable(), "-v", "error", "-i", str(path),
        "-vn", "-ac", "1", "-ar", str(sample_rate),
        "-f", "s16le", "pipe:1",
    ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    audio = np.frombuffer(process.stdout, dtype=np.int16).astype(np.float32)
    if audio.size == 0:
        return np.zeros(1, dtype=np.float32)
    audio /= 32768.0

    whole_seconds = math.ceil(audio.size / sample_rate)
    padded = np.pad(audio, (0, whole_seconds * sample_rate - audio.size))
    frames = padded.reshape(whole_seconds, sample_rate)
    rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
    return rms


def analyze_viral_candidates(path: Path, target_duration: int = 30, limit: int = 5) -> list[dict]:
    """Find exciting candidate moments using audio dynamics.

    This is an intentionally local/offline MVP heuristic. It favors windows with
    sustained loudness plus sudden excitement spikes. A later version can combine
    this with transcript semantics, chat velocity, face emotion, and game events.
    """
    info = probe_video(path)
    rms = _audio_rms_per_second(path)

    if rms.size < 2:
        return [{"start": 0.0, "end": min(float(target_duration), info.duration), "score": 50.0}]

    # Robust normalization prevents one clipping spike from dominating everything.
    lo = float(np.percentile(rms, 10))
    hi = float(np.percentile(rms, 95))
    norm = np.clip((rms - lo) / max(hi - lo, 1e-6), 0, 1)
    spikes = np.clip(np.diff(norm, prepend=norm[0]), 0, 1)

    window = max(8, int(target_duration))
    max_start = max(0, len(norm) - window)
    scored: list[tuple[float, int]] = []
    for start in range(max_start + 1):
        end = min(len(norm), start + window)
        n = norm[start:end]
        s = spikes[start:end]
        # Balance sustained energy, peaks, and sudden changes.
        score = 0.55 * float(np.mean(n)) + 0.30 * float(np.max(n)) + 0.15 * float(np.mean(np.sort(s)[-min(3, len(s)):]))
        scored.append((score, start))

    scored.sort(reverse=True)
    picks: list[dict] = []
    for score, start in scored:
        if any(abs(start - int(p["start"])) < window * 0.8 for p in picks):
            continue
        end = min(info.duration, start + window)
        if end - start < 5:
            continue
        picks.append({
            "start": float(start),
            "end": float(end),
            "score": round(score * 100, 1),
        })
        if len(picks) >= limit:
            break

    if not picks:
        picks = [{"start": 0.0, "end": min(float(target_duration), info.duration), "score": 50.0}]
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


def safe_export_filename(title: str, fallback: str = "ClipPilot Viral Moment") -> str:
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


def generate_viral_title(clip_path: Path, source_title: str = "", caption_text: str = "") -> dict:
    """Generate a concise filename title from the rendered clip and known context.

    The MVP deliberately avoids pretending to understand speech. It inspects the
    finished clip's audio-energy arc and combines that signal with trustworthy
    context already available from the VOD title or creator-provided caption.
    """
    try:
        pattern, hook = _classify_clip_energy(_audio_rms_per_second(clip_path))
    except (OSError, subprocess.SubprocessError, RuntimeError):
        pattern, hook = "surprise", "The Moment I Didn’t See Coming"

    caption = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", caption_text or "")).strip()
    if caption:
        title = caption[:92].strip()
        strategy = "creator_caption"
    else:
        context = _clean_title_context(source_title)
        title = f"{context} — {hook}" if context else hook
        strategy = pattern

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


def _render_square_caption(text: str, destination: Path, font_scale: float = 1.0) -> None:
    if _render_square_caption_macos(text, destination, font_scale):
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
    canvas.save(destination)


def _render_sound_effect(effect: SoundEffect, destination: Path) -> None:
    """Create an original meme-style sound without bundling copyrighted samples."""
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


def _apply_effects(
    source: Path,
    output: Path,
    duration: float,
    width: int,
    height: int,
    sound_effect: SoundEffect,
    visual_effect: VisualEffect,
    effect_time: float,
    sound_volume: float,
    visual_strength: float,
) -> None:
    trigger = min(max(0.0, float(effect_time)), max(0.0, duration - 0.05))
    volume = min(2.0, max(0.0, float(sound_volume)))
    strength = min(1.5, max(0.25, float(visual_strength)))
    temporary_paths: list[Path] = []
    inputs = [ffmpeg_executable(), "-y", "-v", "error", "-i", str(source)]
    sound_index: int | None = None
    overlay_index: int | None = None

    try:
        if sound_effect != "none":
            sound_file = tempfile.NamedTemporaryFile(prefix="clippilot-sfx-", suffix=".wav", delete=False)
            sound_path = Path(sound_file.name)
            sound_file.close()
            temporary_paths.append(sound_path)
            _render_sound_effect(sound_effect, sound_path)
            sound_index = 1
            inputs += ["-i", str(sound_path)]

        if visual_effect in {"lens-flare", "white-flash"}:
            overlay_file = tempfile.NamedTemporaryFile(prefix="clippilot-vfx-", suffix=".png", delete=False)
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

        audio_label: str | None = None
        if sound_index is not None:
            delay_ms = round(trigger * 1000)
            filters.append(f"[{sound_index}:a]adelay={delay_ms}|{delay_ms},volume={volume:.3f}[sfx]")
            if _has_audio(source):
                filters.append("[0:a][sfx]amix=inputs=2:duration=first:dropout_transition=0[aout]")
            else:
                filters.append(f"[sfx]apad=whole_dur={duration:.3f},atrim=0:{duration:.3f}[aout]")
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
    caption_overlay_path: Path | None = None,
    sound_effect: SoundEffect = "none",
    visual_effect: VisualEffect = "none",
    effect_time: float = 1.0,
    sound_volume: float = 1.0,
    visual_strength: float = 1.0,
) -> None:
    info = probe_video(source)
    start = max(0.0, min(start, info.duration))
    end = max(start + 0.1, min(end, info.duration))
    duration = end - start
    if sound_effect not in {"none", "impact-boom", "whoosh", "record-scratch"}:
        raise ValueError("Unknown sound effect.")
    if visual_effect not in {"none", "lens-flare", "punch-zoom", "white-flash"}:
        raise ValueError("Unknown visual effect.")

    has_effects = sound_effect != "none" or visual_effect != "none"
    base_temporary: Path | None = None
    render_target = output
    if has_effects:
        base_file = tempfile.NamedTemporaryFile(prefix="clippilot-base-", suffix=".mp4", delete=False)
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
            filter_complex = (
                f"[0:v]split=2[face][game];"
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
                overlay_file = tempfile.NamedTemporaryFile(prefix="clippilot-caption-", suffix=".png", delete=False)
                overlay_path = Path(overlay_file.name)
                overlay_file.close()
            else:
                overlay_path = caption_overlay_path
            try:
                if owns_overlay:
                    _render_square_caption(caption_text, overlay_path, caption_font_scale)
                filter_complex = (
                    f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
                    f"crop={width}:{height}[base];"
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
            cmd = common + [
                "-vf", vf,
                "-map", "0:v:0", "-map", "0:a?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart",
                str(render_target),
            ]
            _run(cmd)

        if has_effects:
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
                sound_volume,
                visual_strength,
            )
    finally:
        if base_temporary is not None:
            base_temporary.unlink(missing_ok=True)
