from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave
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

# Live captions sit this far above the bottom edge before the creator raises them.
LIVE_CAPTION_BASE_MARGINS = {"landscape": 92, "portrait": 150, "square": 105}
LIVE_CAPTION_BASE_FONT_SIZES = {"landscape": 72, "portrait": 68, "square": 64}
DEFAULT_LIVE_CAPTION_HEIGHT = 0.0
DEFAULT_LIVE_CAPTION_SCALE = 1.0
MIN_LIVE_CAPTION_SCALE = 0.50
MAX_LIVE_CAPTION_SCALE = 1.75


def _frame_shape(width: int, height: int) -> str:
    if width > height:
        return "landscape"
    if height > width:
        return "portrait"
    return "square"


def live_caption_margin(width: int, height: int, height_fraction: float = DEFAULT_LIVE_CAPTION_HEIGHT) -> int:
    """Vertical margin for the caption block, raised from its resting position.

    ``height_fraction`` runs from 0.0, which keeps the captions just above the
    bottom edge, to 1.0, which lifts them to the middle of the frame.
    """
    base = LIVE_CAPTION_BASE_MARGINS[_frame_shape(width, height)]
    lift = max(0.0, min(1.0, float(height_fraction)))
    return base + round(lift * (height / 2 - base))


def live_caption_font_size(width: int, height: int, scale: float = DEFAULT_LIVE_CAPTION_SCALE) -> int:
    """Caption type size for the frame, scaled by the creator's size slider."""
    base = LIVE_CAPTION_BASE_FONT_SIZES[_frame_shape(width, height)]
    clamped = max(MIN_LIVE_CAPTION_SCALE, min(MAX_LIVE_CAPTION_SCALE, float(scale)))
    return max(12, round(base * clamped))


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


# Silero voice-activity detection, bundled beside Whisper, finds where someone is
# speaking even over game audio and music.
VAD_MODEL_NAME = "ggml-silero-v6.2.0.bin"
_VAD_CHUNK_SECONDS = 600.0
_VAD_CHUNK_OVERLAP_SECONDS = 4.0
_VAD_SEGMENT_LINE = re.compile(r"Speech segment \d+: start = ([0-9.]+), end = ([0-9.]+)")


def speech_detector_paths() -> tuple[Path, Path]:
    executable_name = "whisper-vad-speech-segments.exe" if os.name == "nt" else "whisper-vad-speech-segments"
    for root in _runtime_candidates():
        tool = root / "bin" / executable_name
        model = root / "models" / VAD_MODEL_NAME
        if tool.is_file() and model.is_file():
            return tool, model
    fallback = _runtime_candidates()[0]
    return fallback / "bin" / executable_name, fallback / "models" / VAD_MODEL_NAME


def caption_engine_status() -> dict[str, object]:
    cli, model = caption_runtime_paths()
    tool, vad_model = speech_detector_paths()
    return {
        "available": cli.is_file() and model.is_file(),
        "engine": "Whisper.cpp",
        "model": "base.en",
        "speech_detector": tool.is_file() and vad_model.is_file(),
    }


def _merge_segments(segments: list[tuple[float, float]], duration: float) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in sorted(segments):
        start, end = max(0.0, start), min(duration, end)
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(round(start, 3), round(end, 3)) for start, end in merged]


def _run_speech_detector(tool: Path, model: Path, audio: Path) -> list[tuple[float, float]]:
    result = subprocess.run(
        [
            # The Silero model is tiny: one thread runs it 2.4x faster than eight
            # (1.2 s against 2.9 s for ten minutes on an M2 Max) and leaves the
            # other cores to Whisper, which transcribes at the same time.
            str(tool), "-vm", str(model), "-f", str(audio), "-t", "1",
            # Keep short replies such as "yeah" as speech, and report every gap.
            "-vspd", "100", "-vsd", "100", "-np",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
        timeout=600,
        env=_runtime_environment(tool),
    )
    output = result.stdout.decode("utf-8", errors="replace")
    if result.returncode != 0 or "Detected" not in output:
        lines = output.strip().splitlines()
        raise RuntimeError(f"Speech detection failed: {lines[-1] if lines else 'no output'}")
    # The tool reports centiseconds.
    return [(float(start) / 100.0, float(end) / 100.0) for start, end in _VAD_SEGMENT_LINE.findall(output)]


def detect_speech_segments(source: Path, start: float, end: float, ffmpeg: str) -> list[tuple[float, float]]:
    """Stretches where someone is speaking, in seconds from `start`.

    The recording is analysed in ten-minute pieces that overlap slightly, so a
    multi-hour VOD never has to be held in memory at once.
    """
    tool, model = speech_detector_paths()
    if not tool.is_file() or not model.is_file():
        raise ValueError("The bundled speech detector is missing. Reinstall the latest Clip Farm Pilot release.")
    duration = max(0.1, float(end) - float(start))
    segments: list[tuple[float, float]] = []
    temporary_dir = Path(tempfile.mkdtemp(prefix=f"{APP_SLUG}-speech-"))
    try:
        piece_start = 0.0
        while piece_start < duration:
            piece_length = min(_VAD_CHUNK_SECONDS + _VAD_CHUNK_OVERLAP_SECONDS, duration - piece_start)
            audio = temporary_dir / "piece.wav"
            decoded = subprocess.run(
                [
                    ffmpeg, "-y", "-v", "error",
                    "-ss", f"{max(0.0, float(start)) + piece_start:.3f}", "-i", str(source),
                    "-t", f"{piece_length:.3f}", "-vn", "-ar", "16000", "-ac", "1",
                    "-c:a", "pcm_s16le", str(audio),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if decoded.returncode != 0 or not audio.is_file():
                raise RuntimeError("The audio could not be prepared for speech detection.")
            segments.extend(
                (piece_start + segment_start, piece_start + segment_end)
                for segment_start, segment_end in _run_speech_detector(tool, model, audio)
            )
            piece_start += _VAD_CHUNK_SECONDS
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Speech detection timed out.") from exc
    finally:
        shutil.rmtree(temporary_dir, ignore_errors=True)
    return _merge_segments(segments, duration)


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
    speech_detector_self_test()


def speech_detector_self_test() -> None:
    """Run the bundled Silero model on a short silent clip to prove it loads."""
    tool, model = speech_detector_paths()
    if not tool.is_file() or not model.is_file():
        raise RuntimeError("The bundled speech detector is missing.")
    with tempfile.TemporaryDirectory(prefix=f"{APP_SLUG}-speech-test-") as temporary:
        audio = Path(temporary) / "silence.wav"
        with wave.open(str(audio), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b"\x00\x00" * 16000)
        try:
            segments = _run_speech_detector(tool, model, audio)
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            raise RuntimeError(f"The bundled speech detector could not run: {exc}") from exc
    if segments:
        raise RuntimeError("The bundled speech detector heard speech in silence.")


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


# Whisper's DTW alignment marks each word about 0.13 s after it actually starts;
# measured across six voices and three kinds of background audio.
DTW_ONSET_LEAD_SECONDS = 0.13


def _dtw_onset(item: dict) -> float | None:
    """The DTW-aligned start of a transcribed word, or None when DTW is unavailable."""
    for token in item.get("tokens", []) or []:
        if not isinstance(token, dict):
            continue
        text = str(token.get("text", ""))
        if not text.strip() or text.startswith("[_"):
            continue
        try:
            moment = float(token.get("t_dtw", -1))
        except (TypeError, ValueError):
            return None
        return None if moment < 0 else max(0.0, moment / 100.0 - DTW_ONSET_LEAD_SECONDS)
    return None


def _spoken_length(text: str) -> float:
    """A rough upper bound on how long a word lasts, used when a pause follows it."""
    letters = sum(1 for character in text if character.isalnum())
    return max(0.20, 0.09 * letters + 0.12)


def parse_whisper_words(payload: dict, duration: float) -> list[CaptionWord]:
    clip_duration = max(0.0, float(duration))
    entries: list[tuple[str, float, float, float | None]] = []
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
        entries.append((text, start, end, _dtw_onset(item)))

    if entries and all(onset is not None for _, _, _, onset in entries):
        return _words_from_dtw(entries, clip_duration)

    words: list[CaptionWord] = []
    for text, start, end, _ in entries:
        if end <= start:
            end = min(clip_duration, start + 0.12)
        if end <= start or start >= clip_duration:
            continue
        if words and start < words[-1].start:
            continue
        words.append(CaptionWord(text=text, start=round(start, 3), end=round(end, 3)))
    return words


def _words_from_dtw(entries: list[tuple[str, float, float, float | None]], duration: float) -> list[CaptionWord]:
    """Place words by their DTW onsets: each lasts until the next begins, or its likely length."""
    onsets: list[tuple[str, float]] = []
    for text, _, _, onset in entries:
        if onset is None or onset >= duration:
            continue
        if onsets and onset < onsets[-1][1]:
            continue
        onsets.append((text, onset))
    words: list[CaptionWord] = []
    for index, (text, start) in enumerate(onsets):
        following = onsets[index + 1][1] if index + 1 < len(onsets) else duration
        end = min(following, start + _spoken_length(text), duration)
        if end - start < 0.04:
            end = min(duration, start + 0.04)
        if end <= start:
            continue
        words.append(CaptionWord(text=text, start=round(start, 3), end=round(end, 3)))
    return words


# Whisper tends to write clean transcripts and leave hesitations out. Starting it
# from a sample that keeps them makes it write "um" and "uh" down, which filler
# removal needs; it is only used then, so ordinary captions stay as they were.
FILLER_PROMPT = "So, um, I was, uh, thinking we could, um, try this. Uh, yeah, okay."


def transcribe_words(
    source: Path,
    start: float,
    end: float,
    ffmpeg: str,
    include_fillers: bool = False,
) -> list[CaptionWord]:
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
            "-l", "en", "-t", str(thread_count), "-np",
            # DTW aligns each word to the audio far more closely than Whisper's own
            # timestamps. It needs flash attention off and the full JSON output.
            "-nfa", "-dtw", "base.en", "-ojf",
            "-ml", "1", "-sow", "-of", str(result_base),
        ]
        if include_fillers:
            command += ["--prompt", FILLER_PROMPT]
        timeout_seconds = max(90.0, min(1800.0, duration * 8.0))
        result_path = result_base.with_suffix(".json")
        # On a Mac the engine runs on the GPU through Metal: 1.7x faster than eight
        # CPU threads, with the same words and cut placement on the pause and filler
        # benchmark. Elsewhere, and if the GPU run fails, it runs on the CPU.
        attempts = [[], ["-ng"]] if sys.platform == "darwin" else [["-ng"]]
        for extra in attempts:
            transcription = subprocess.run(
                command + extra,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout_seconds,
                env=_runtime_environment(cli),
            )
            if transcription.returncode == 0 and result_path.is_file():
                break
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
    height_fraction: float = DEFAULT_LIVE_CAPTION_HEIGHT,
    scale: float = DEFAULT_LIVE_CAPTION_SCALE,
) -> None:
    try:
        base_hex, highlight_hex = LIVE_CAPTION_SCHEMES[scheme]
    except KeyError as exc:
        raise ValueError("Unknown live-caption colour scheme.") from exc

    base_color = _ass_color(base_hex)
    highlight_color = _ass_color(highlight_hex)
    font_size = live_caption_font_size(width, height, scale)
    margin_vertical = live_caption_margin(width, height, height_fraction)
    # Keep the outline in proportion so scaled-up type does not lose its edge.
    outline_scale = font_size / LIVE_CAPTION_BASE_FONT_SIZES[_frame_shape(width, height)]
    outline = max(2, round((5 if min(width, height) >= 1000 else 4) * outline_scale))
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
