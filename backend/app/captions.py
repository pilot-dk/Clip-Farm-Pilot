from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .brand import APP_SLUG, env


LiveCaptionScheme = Literal["pilot-lime", "ocean", "sunset", "neon-pink", "violet"]

LIVE_CAPTION_SCHEMES: dict[LiveCaptionScheme, tuple[str, str]] = {
    "pilot-lime": ("#FFFFFF", "#B9F34A"),
    "ocean": ("#FFFFFF", "#35DCFF"),
    "sunset": ("#FFFFFF", "#FFD24A"),
    "neon-pink": ("#FFFFFF", "#FF4FD8"),
    "violet": ("#FFFFFF", "#A98BFF"),
}


@dataclass(frozen=True)
class CaptionWord:
    text: str
    start: float
    end: float


def _runtime_candidates() -> list[Path]:
    candidates: list[Path] = []
    configured = env("CAPTION_RUNTIME_DIR")
    if configured:
        candidates.append(Path(str(configured)).expanduser())
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.append(Path(bundle_root) / "caption_runtime")
    candidates.append(Path(__file__).resolve().parents[2] / ".caption-runtime")
    return candidates


def caption_runtime_paths() -> tuple[Path, Path]:
    configured_cli = env("WHISPER_CLI")
    configured_model = env("WHISPER_MODEL")
    if configured_cli and configured_model:
        return Path(str(configured_cli)).expanduser(), Path(str(configured_model)).expanduser()

    executable_name = "whisper-cli.exe" if os.name == "nt" else "whisper-cli"
    for root in _runtime_candidates():
        cli = root / "bin" / executable_name
        model = root / "models" / "ggml-base.en.bin"
        if cli.is_file() and model.is_file():
            return cli, model
    fallback = _runtime_candidates()[0]
    return fallback / "bin" / executable_name, fallback / "models" / "ggml-base.en.bin"


def caption_engine_status() -> dict[str, object]:
    cli, model = caption_runtime_paths()
    return {
        "available": cli.is_file() and model.is_file(),
        "engine": "Whisper.cpp",
        "model": "base.en",
    }


def caption_engine_self_test() -> None:
    """Confirm that the packaged executable and its shared libraries can launch."""
    cli, model = caption_runtime_paths()
    if not cli.is_file() or not model.is_file():
        raise RuntimeError("The bundled offline live-caption engine is missing.")
    try:
        result = subprocess.run(
            [str(cli), "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=20,
            env=_runtime_environment(cli),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("The bundled offline live-caption engine could not start.") from exc
    output = result.stdout.decode("utf-8", errors="replace").lower()
    if result.returncode != 0 or "whisper" not in output:
        raise RuntimeError("The bundled offline live-caption engine did not pass its startup test.")


def _runtime_environment(cli: Path) -> dict[str, str]:
    environment = os.environ.copy()
    binary_dir = str(cli.parent)
    if os.name == "nt":
        environment["PATH"] = binary_dir + os.pathsep + environment.get("PATH", "")
    elif sys.platform == "darwin":
        environment["DYLD_LIBRARY_PATH"] = binary_dir + os.pathsep + environment.get("DYLD_LIBRARY_PATH", "")
    else:
        environment["LD_LIBRARY_PATH"] = binary_dir + os.pathsep + environment.get("LD_LIBRARY_PATH", "")
    return environment


def _clean_transcribed_word(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text or text.startswith("[_") or text.endswith("_]"):
        return ""
    return text.replace("{", "").replace("}", "")[:48]


def parse_whisper_words(payload: dict, duration: float) -> list[CaptionWord]:
    words: list[CaptionWord] = []
    clip_duration = max(0.0, float(duration))
    for item in payload.get("transcription", []):
        if not isinstance(item, dict):
            continue
        text = _clean_transcribed_word(item.get("text"))
        offsets = item.get("offsets", {})
        if not text or not isinstance(offsets, dict):
            continue
        try:
            start = max(0.0, float(offsets.get("from", 0)) / 1000.0)
            end = min(clip_duration, float(offsets.get("to", 0)) / 1000.0)
        except (TypeError, ValueError):
            continue
        if end <= start:
            end = min(clip_duration, start + 0.12)
        if end <= start or start >= clip_duration:
            continue
        if words and start < words[-1].start:
            continue
        words.append(CaptionWord(text=text, start=round(start, 3), end=round(end, 3)))
    return words


def transcribe_words(source: Path, start: float, end: float, ffmpeg: str) -> list[CaptionWord]:
    cli, model = caption_runtime_paths()
    if not cli.is_file() or not model.is_file():
        raise ValueError(
            "Live captions are not available in this build. Reinstall the latest Clip Farm Pilot release."
        )

    duration = max(0.1, float(end) - float(start))
    temporary_dir = Path(tempfile.mkdtemp(prefix=f"{APP_SLUG}-captions-"))
    audio_path = temporary_dir / "speech.wav"
    result_base = temporary_dir / "words"
    try:
        audio_result = subprocess.run(
            [
                ffmpeg, "-y", "-v", "error",
                "-ss", f"{max(0.0, float(start)):.3f}", "-i", str(source),
                "-t", f"{duration:.3f}", "-vn", "-ar", "16000", "-ac", "1",
                "-c:a", "pcm_s16le", str(audio_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if audio_result.returncode != 0 or not audio_path.is_file() or audio_path.stat().st_size < 128:
            return []

        thread_count = max(1, min(8, os.cpu_count() or 4))
        command = [
            str(cli), "-m", str(model), "-f", str(audio_path),
            "-l", "en", "-t", str(thread_count), "-ng", "-np",
            "-oj", "-ml", "1", "-sow", "-of", str(result_base),
        ]
        timeout_seconds = max(90.0, min(1800.0, duration * 8.0))
        transcription = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
            env=_runtime_environment(cli),
        )
        result_path = result_base.with_suffix(".json")
        if transcription.returncode != 0 or not result_path.is_file():
            message = transcription.stderr.decode("utf-8", errors="replace").strip().splitlines()
            detail = message[-1] if message else "The offline speech engine did not finish."
            raise RuntimeError(f"Live-caption transcription failed: {detail}")
        with result_path.open("r", encoding="utf-8") as handle:
            return parse_whisper_words(json.load(handle), duration)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Live-caption transcription timed out. Try exporting a shorter clip.") from exc
    finally:
        shutil.rmtree(temporary_dir, ignore_errors=True)


def _ass_color(hex_color: str) -> str:
    value = hex_color.lstrip("#")
    if len(value) != 6 or not re.fullmatch(r"[0-9A-Fa-f]{6}", value):
        raise ValueError("Invalid live-caption colour.")
    red, green, blue = value[0:2], value[2:4], value[4:6]
    return f"&H00{blue}{green}{red}&".upper()


def _ass_time(seconds: float) -> str:
    centiseconds = max(0, round(float(seconds) * 100))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    whole_seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{fraction:02d}"


def _ass_text(value: str) -> str:
    return value.replace("\\", "／").replace("{", "(").replace("}", ")").replace("\n", " ")


def group_caption_words(words: list[CaptionWord]) -> list[list[CaptionWord]]:
    groups: list[list[CaptionWord]] = []
    current: list[CaptionWord] = []
    for word in words:
        proposed = current + [word]
        character_count = len(" ".join(item.text for item in proposed))
        gap = word.start - current[-1].end if current else 0.0
        if current and (len(current) >= 5 or character_count > 34 or gap > 0.75):
            groups.append(current)
            current = [word]
        else:
            current = proposed
        if current and re.search(r"[.!?][\"']?$", current[-1].text) and len(current) >= 2:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def write_live_caption_ass(
    words: list[CaptionWord],
    destination: Path,
    width: int,
    height: int,
    scheme: LiveCaptionScheme,
) -> None:
    try:
        base_hex, highlight_hex = LIVE_CAPTION_SCHEMES[scheme]
    except KeyError as exc:
        raise ValueError("Unknown live-caption colour scheme.") from exc

    base_color = _ass_color(base_hex)
    highlight_color = _ass_color(highlight_hex)
    font_size = 72 if width > height else 68 if height > width else 64
    margin_vertical = 92 if width > height else 150 if height > width else 105
    outline = 5 if min(width, height) >= 1000 else 4
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Live,Arial Black,{font_size},{base_color},{highlight_color},&H00000000,&H64000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline},2,2,70,70,{margin_vertical},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events: list[str] = []
    for group in group_caption_words(words):
        for index, word in enumerate(group):
            event_start = word.start
            event_end = word.end
            if index + 1 < len(group):
                event_end = max(event_end, group[index + 1].start)
            event_end = max(event_start + 0.08, event_end)
            pieces = []
            for piece_index, piece in enumerate(group):
                color = highlight_color if piece_index == index else base_color
                pieces.append(f"{{\\c{color}}}{_ass_text(piece.text)}")
            events.append(
                f"Dialogue: 0,{_ass_time(event_start)},{_ass_time(event_end)},Live,,0,0,0,,"
                + " ".join(pieces)
            )
    destination.write_text(header + "\n".join(events) + ("\n" if events else ""), encoding="utf-8")
