"""Auto captions: an on-device vision-language model writes a square clip's caption.

It names the moment the way a fan page would -- "Bro shows off his rescued
puppy 🥹❤️‍🩹" -- from four frames of the clip and the words spoken in it. The
model, Qwen3.5 4B with its vision encoder, is run by llama.cpp's llama-server,
which ships with the app. The model is downloaded once, the first time Auto is
used, and everything after that runs offline.

Small models make three kinds of mistake, and each step guards against one:
they read a picture better when asked only to describe it, so the frames are
described first; they latch on to any example caption and copy it, so the
caption prompt has none; and one sample can misread the clip ("dog" said as
slang becomes a dog), so six candidates are written at once and the one whose
words the others agree with wins.
"""
from __future__ import annotations

import atexit
import base64
import hashlib
import json
import math
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .brand import APP_NAME, APP_VERSION, env
from .captions import _runtime_candidates
from .profanity import is_profane


@dataclass(frozen=True)
class ModelFile:
    name: str
    size: int
    sha256: str


MODEL_NAME = "Qwen3.5 4B"
# Pinned to one commit of the repository, and each file to its checksum, so the
# download is always exactly the model the captions were tuned on.
MODEL_URL = "https://huggingface.co/unsloth/Qwen3.5-4B-GGUF/resolve/e87f176479d0855a907a41277aca2f8ee7a09523"
MODEL_FILES = (
    ModelFile("Qwen3.5-4B-Q4_K_M.gguf", 2_740_937_888, "00fe7986ff5f6b463e62455821146049db6f9313603938a70800d1fb69ef11a4"),
    ModelFile("mmproj-F16.gguf", 672_423_616, "cd88edcf8d031894960bb0c9c5b9b7e1fea6ebee02b9f7ce925a00d12891f864"),
)
_DOWNLOAD_CHUNK = 1024 * 1024
_SPARE_DISK = 512 * 1024 * 1024  # Left free after the download.

# Each caption is written from four frames, a quarter of the way into each
# quarter of the clip, each shrunk to fit 448 pixels: enough to read a scene,
# and 256 image tokens apiece keep the prompt short.
FRAME_SHARES = (0.12, 0.37, 0.62, 0.87)
_FRAME_SIZE = 448
_IMAGE_TOKENS = 256
_CANDIDATES = 6
_SPOKEN_LIMIT = 600   # Characters of the transcript the model reads.
_SERVER_CONTEXT = 12_288  # Shared by the six parallel slots: 2,048 tokens each.
_SERVER_START_SECONDS = 240.0
_SERVER_IDLE_SECONDS = 300.0

MOOD_EMOJI = {
    "wholesome": "🥹❤️‍🩹",
    "funny": "😭💔🥀",
    "hype": "🔥",
    "win": "🏆🔥",
    "shocking": "😳",
}

DESCRIBE_PROMPT = (
    "These are four frames from a short video, in order. Words spoken in it: \"{spoken}\"\n\n"
    "In one or two sentences, describe what is happening: the setting, what the people are doing, and how "
    "they seem to feel. Call people a man, woman, boy or girl when that is clear. Do not guess who anyone is "
    "or give names."
)
CAPTION_PROMPT = """You are the caption writer for a viral fan page that posts short clips of streamers and creators.

You get a description of a clip and the words spoken in it. Work out the single main moment of the clip and its mood, then write the caption shown on the video.

The caption:
- sums up the moment the way a fan page would, in 3 to 8 words, in the present tense;
- talks about what the person does or what happens to them, not a quote of what they say;
- calls one man "Bro" or "He" and one woman "She"; two or more people are "They"; if it is not clear who it is, it says "Bro"; it uses a name only if the name is spoken in the clip;
- treats "chat" as the stream's viewers, not a person;
- never invents anything the description and the words do not support, and never swears;
- may put one word in capitals for emphasis, such as NOT or NEVER;
- has no emojis, quotation marks or hashtags.

Mood is one of: wholesome (sweet, heartwarming, emotional), funny (a fail, embarrassing, chaotic), hype (impressive, exciting, intense), win (an achievement, award, victory), shocking (surprising, scary)."""
CAPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "moment": {"type": "string"},
        "mood": {"type": "string", "enum": list(MOOD_EMOJI)},
        "caption": {"type": "string"},
    },
    "required": ["moment", "mood", "caption"],
}


class CaptionModelError(RuntimeError):
    """The caption model or its engine could not do what was asked."""


def llama_server_path() -> Path:
    """The bundled llama-server, kept apart from Whisper's tools: each ships its own ggml libraries."""
    configured = env("LLAMA_SERVER")
    if configured:
        return Path(str(configured)).expanduser()
    name = "llama-server.exe" if os.name == "nt" else "llama-server"
    candidates = [root / "llm" / name for root in _runtime_candidates()]
    return next((candidate for candidate in candidates if candidate.is_file()), candidates[0])


def engine_available() -> bool:
    return llama_server_path().is_file()


def _engine_environment(executable: Path) -> dict[str, str]:
    """The environment llama-server runs in: its own folder first on the library path.

    A packaged app puts its own libraries on the path too, and some carry the
    same names as llama.cpp's, from Whisper.
    """
    environment = os.environ.copy()
    variable = "PATH" if os.name == "nt" else "DYLD_LIBRARY_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH"
    environment[variable] = str(executable.parent) + os.pathsep + environment.get(variable, "")
    return environment


def engine_self_test() -> str:
    """Start the bundled llama-server just far enough to print its version, proving its libraries load."""
    executable = llama_server_path()
    if not executable.is_file():
        raise CaptionModelError("The bundled caption engine is missing.")
    try:
        result = subprocess.run(
            [str(executable), "--version"], capture_output=True, text=True, timeout=60, check=False,
            env=_engine_environment(executable), creationflags=0x08000000 if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CaptionModelError(f"The bundled caption engine could not start: {exc}") from exc
    output = f"{result.stdout}\n{result.stderr}"
    version = next((line.strip() for line in output.splitlines() if line.strip().startswith("version:")), "")
    if result.returncode != 0 or not version:
        raise CaptionModelError("The bundled caption engine did not pass its startup test.")
    return version


class CaptionModel:
    """The caption model on disk: whether it is there, and downloading it once."""

    def __init__(self, directory: Path, base_url: str = MODEL_URL, files: tuple[ModelFile, ...] = MODEL_FILES):
        self.directory = Path(directory)
        self.base_url = base_url.rstrip("/")
        self.files = files
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._downloaded = 0
        self._error = ""

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.files)

    def path(self, item: ModelFile) -> Path:
        return self.directory / item.name

    def _marker(self) -> Path:
        return self.directory / "verified.json"

    def _verified(self) -> dict[str, str]:
        try:
            payload = json.loads(self._marker().read_text(encoding="utf-8"))
            return {str(key): str(value) for key, value in payload.items()} if isinstance(payload, dict) else {}
        except (OSError, ValueError):
            return {}

    def _file_ready(self, item: ModelFile, verified: dict[str, str]) -> bool:
        path = self.path(item)
        return verified.get(item.name) == item.sha256 and path.is_file() and path.stat().st_size == item.size

    def ready(self) -> bool:
        verified = self._verified()
        return all(self._file_ready(item, verified) for item in self.files)

    def downloading(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _bytes_on_disk(self) -> int:
        """What is downloaded so far, counting partial files, so progress survives a restart."""
        verified = self._verified()
        total = 0
        for item in self.files:
            if self._file_ready(item, verified):
                total += item.size
            elif self.path(item).with_suffix(".part").is_file():
                total += min(self.path(item).with_suffix(".part").stat().st_size, item.size)
        return total

    def status(self) -> dict[str, object]:
        with self._lock:
            if self.downloading():
                state = "downloading"
            elif self.ready():
                state = "ready"
            elif self._error:
                state = "error"
            else:
                state = "missing"
            downloaded = {"ready": self.total_bytes, "downloading": self._downloaded}.get(state)
            return {
                "state": state,
                "model": MODEL_NAME,
                "downloaded_bytes": self._bytes_on_disk() if downloaded is None else downloaded,
                "total_bytes": self.total_bytes,
                "error": self._error if state == "error" else "",
            }

    def start_download(self) -> dict[str, object]:
        with self._lock:
            if not self.downloading() and not self.ready():
                self._cancel.clear()
                self._error = ""
                self._thread = threading.Thread(target=self._download_all, name="caption-model-download", daemon=True)
                self._thread.start()
        return self.status()

    def cancel(self) -> dict[str, object]:
        self._cancel.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=30)
        return self.status()

    def _download_all(self) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            verified = self._verified()
            pending = [item for item in self.files if not self._file_ready(item, verified)]
            done = sum(item.size for item in self.files if item not in pending)
            partial = sum(
                min(self.path(item).with_suffix(".part").stat().st_size, item.size)
                for item in pending if self.path(item).with_suffix(".part").is_file()
            )
            self._downloaded = done + partial
            needed = sum(item.size for item in pending) - partial
            free = shutil.disk_usage(self.directory).free
            if needed + _SPARE_DISK > free:
                raise CaptionModelError(
                    f"Auto captions need {needed / 1e9:.1f} GB of free disk space for the caption model; "
                    f"{free / 1e9:.1f} GB is free."
                )
            for item in pending:
                self._download_file(item, done)
                done += item.size
                verified[item.name] = item.sha256
                temporary = self._marker().with_suffix(".tmp")
                temporary.write_text(json.dumps(verified, indent=1) + "\n", encoding="utf-8")
                temporary.replace(self._marker())
            self._downloaded = self.total_bytes
        except CaptionModelError as exc:
            # Cancelling is a pause, not a failure: the next download resumes.
            self._error = "" if self._cancel.is_set() else str(exc)
        except (OSError, urllib.error.URLError, TimeoutError) as exc:
            self._error = f"The caption model could not be downloaded: {getattr(exc, 'reason', exc)}"

    def _download_file(self, item: ModelFile, done_before: int) -> None:
        """Download one file, resuming a partial one, and keep it only if its checksum matches."""
        target = self.path(item)
        partial = target.with_suffix(".part")
        digest = hashlib.sha256()
        have = partial.stat().st_size if partial.is_file() else 0
        if have > item.size:
            partial.unlink()
            have = 0
        if have:
            with partial.open("rb") as handle:
                for chunk in iter(lambda: handle.read(_DOWNLOAD_CHUNK), b""):
                    digest.update(chunk)
        if have < item.size:
            request = urllib.request.Request(
                f"{self.base_url}/{item.name}",
                headers={"User-Agent": f"{APP_NAME.replace(' ', '-')}/{APP_VERSION}"},
            )
            if have:
                request.add_header("Range", f"bytes={have}-")
            with urllib.request.urlopen(request, timeout=60) as response:
                if have and response.status != 206:
                    # The server sent the whole file instead of the rest: start over.
                    have = 0
                    digest = hashlib.sha256()
                with partial.open("ab" if have else "wb") as output:
                    while True:
                        if self._cancel.is_set():
                            raise CaptionModelError("The caption model download was cancelled.")
                        chunk = response.read(_DOWNLOAD_CHUNK)
                        if not chunk:
                            break
                        output.write(chunk)
                        digest.update(chunk)
                        have += len(chunk)
                        self._downloaded = done_before + min(have, item.size)
        if have != item.size or digest.hexdigest() != item.sha256:
            partial.unlink(missing_ok=True)
            raise CaptionModelError(f"The caption model download was damaged ({item.name}). Try again.")
        partial.replace(target)

    def remove(self) -> None:
        self.cancel()
        for item in self.files:
            self.path(item).unlink(missing_ok=True)
            self.path(item).with_suffix(".part").unlink(missing_ok=True)
        self._marker().unlink(missing_ok=True)
        self._error = ""
        self._downloaded = 0


def _process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, check=False, creationflags=0x08000000,
        )
        return "llama-server" in result.stdout.lower()
    result = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True, check=False)
    return "llama-server" in result.stdout


class LlamaServer:
    """One llama-server for the caption model, started on demand and stopped when idle.

    Loading the model takes a few seconds, so the server stays up between clips
    of a bulk export, and closes after five idle minutes to give back the 4 GB
    or so of memory it holds. A server left behind by a crash is stopped the
    next time one starts.
    """

    def __init__(self, model: CaptionModel):
        self.model = model
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._port = 0
        self._key = ""
        self._idle: threading.Timer | None = None

    def _pid_file(self) -> Path:
        return self.model.directory / "llama-server.pid"

    def stop_leftover(self) -> None:
        try:
            pid = int(self._pid_file().read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return
        if _process_running(pid):
            try:
                os.kill(pid, 15 if os.name != "nt" else 9)
            except OSError:
                pass
        self._pid_file().unlink(missing_ok=True)

    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _start(self) -> None:
        executable = llama_server_path()
        if not executable.is_file():
            raise CaptionModelError("The bundled caption engine is missing. Reinstall the latest Clip Farm Pilot release.")
        if not self.model.ready():
            raise CaptionModelError("Download the caption model first.")
        self.stop_leftover()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self._port = probe.getsockname()[1]
        self._key = secrets.token_urlsafe(24)
        model_file, projector = (self.model.path(item) for item in self.model.files)
        log_path = self.model.directory / "llama-server.log"
        environment = _engine_environment(executable)
        command = [
            str(executable), "-m", str(model_file), "--mmproj", str(projector),
            "--host", "127.0.0.1", "--port", str(self._port), "--api-key", self._key,
            "-c", str(_SERVER_CONTEXT), "-np", str(_CANDIDATES), "--image-max-tokens", str(_IMAGE_TOKENS),
            "--no-webui",
        ]
        with log_path.open("wb") as log:
            self._process = subprocess.Popen(
                command, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=environment,
                # A console tool started from a windowed app would open its own
                # console window on Windows for as long as it runs.
                creationflags=0x08000000 if os.name == "nt" else 0,
            )
        self._pid_file().write_text(str(self._process.pid), encoding="utf-8")
        deadline = time.monotonic() + _SERVER_START_SECONDS
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self._port}/health", timeout=2) as response:
                    if response.status == 200:
                        return
            except (OSError, urllib.error.URLError):
                time.sleep(0.25)
        tail = ""
        try:
            tail = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-1]
        except (OSError, IndexError):
            pass
        self.stop()
        raise CaptionModelError(f"The caption engine could not start{': ' + tail if tail else '.'}")

    def chat(self, body: dict) -> list[dict]:
        """Send one chat request, starting the server if needed, and return its choices."""
        with self._lock:
            if self._idle is not None:
                self._idle.cancel()
            if not self.running():
                self._start()
            request = urllib.request.Request(
                f"http://127.0.0.1:{self._port}/v1/chat/completions",
                data=json.dumps({"chat_template_kwargs": {"enable_thinking": False}, **body}).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._key}"},
            )
            try:
                with urllib.request.urlopen(request, timeout=900) as response:
                    choices = json.load(response).get("choices") or []
            except (OSError, urllib.error.URLError, ValueError) as exc:
                raise CaptionModelError(f"The caption engine did not answer: {getattr(exc, 'reason', exc)}") from exc
            finally:
                self._idle = threading.Timer(_SERVER_IDLE_SECONDS, self.stop)
                self._idle.daemon = True
                self._idle.start()
            return choices

    def stop(self) -> None:
        process = self._process
        self._process = None
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        self._pid_file().unlink(missing_ok=True)


def clip_frames(source: Path, start: float, end: float, ffmpeg: str) -> list[bytes]:
    """JPEG frames from across the clip, each fitted inside a 448 pixel square."""
    duration = max(0.1, float(end) - float(start))
    frames: list[bytes] = []
    for share in FRAME_SHARES:
        result = subprocess.run(
            [
                ffmpeg, "-v", "error", "-ss", f"{float(start) + duration * share:.3f}", "-i", str(source),
                "-frames:v", "1", "-vf", f"scale={_FRAME_SIZE}:{_FRAME_SIZE}:force_original_aspect_ratio=decrease",
                "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "3", "-",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, timeout=120,
        )
        if result.returncode == 0 and result.stdout:
            frames.append(result.stdout)
    if not frames:
        raise CaptionModelError("No frames could be read from this clip.")
    return frames


_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF☀-➿⬀-⯿←-⇿⌀-⏿︎️‍⃣]"
)
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’]*")
_FIRST_PERSON = {"i", "i'm", "im", "i've", "i'll", "me", "my", "mine", "we", "we're", "our", "ours", "us"}
_META = {"clip", "video", "caption", "description", "transcript", "frame", "frames", "viewer", "viewers"}
# "Bro", "She" and the like are how the caption refers to people. Written later
# in the line with a capital, they are being used as if they were names: "She
# yells at Bro", "Bro and She react".
_PRONOUN_AS_NAME = re.compile(r"(?<=\s)(?:Bro|She|He|They)\b")
_STOPWORDS = set(
    "bro he she they his her their him them the a an is are was to for of with and in on at while it this that "
    "its as by from into about after before over just so very".split()
)


def clean_caption(text: str) -> str:
    """A caption as the model wrote it, without quotes, emojis, hashtags or a closing full stop."""
    value = _EMOJI.sub("", str(text or ""))
    value = re.sub(r"#\w+", "", value)
    value = re.sub(r"\s+", " ", value).strip().strip("\"'“”‘’").strip()
    value = value.rstrip(".,;:").strip()
    return value[:1].upper() + value[1:] if value else ""


def caption_is_usable(caption: str, spoken: str, scene: str = "") -> bool:
    """Whether a caption reads like a fan page's and stays within what the clip shows and says."""
    words = _WORD.findall(caption)
    if not 2 <= len(words) <= 10:
        return False
    lowered = [word.lower().replace("’", "'") for word in words]
    if any(word in _FIRST_PERSON or word in _META or is_profane(word) for word in lowered):
        return False
    if _PRONOUN_AS_NAME.search(caption):
        return False
    # A name has to come from the clip: one the model made up, or guessed from a
    # face, is not written on someone else's video.
    known = {word.lower() for word in _WORD.findall(f"{spoken} {scene}")}
    for word in words[1:]:
        if word[:1].isupper() and not word.isupper() and word.lower() not in known:
            return False
    return True


def _content_words(caption: str) -> set[str]:
    return {
        word.lower().rstrip("s")
        for word in _WORD.findall(caption)
        if len(word) > 2 and word.lower() not in _STOPWORDS
    }


def rank_captions(candidates: list[tuple[str, str]], spoken: str, scene: str = "") -> tuple[list[str], str]:
    """The usable captions, best first, and the mood most of the candidates agree on.

    A caption scores by how much of its meaning the other candidates share --
    a one-off misreading has nobody agreeing with it -- with a small penalty
    for running long or coming out too short to say anything.
    """
    seen: set[str] = set()
    usable: list[tuple[str, str]] = []
    for text, mood in candidates:
        caption = clean_caption(text)
        if caption and caption.casefold() not in seen and caption_is_usable(caption, spoken, scene):
            seen.add(caption.casefold())
            usable.append((caption, mood if mood in MOOD_EMOJI else "hype"))
    if not usable:
        return [], ""
    words = [_content_words(caption) for caption, _ in usable]

    def score(index: int) -> float:
        mine = words[index]
        agreement = sum(
            len(mine & other) / math.sqrt(len(mine) * len(other))
            for position, other in enumerate(words)
            if position != index and mine and other
        )
        length = len(_WORD.findall(usable[index][0]))
        return agreement - 0.15 * max(0, length - 7) - 0.3 * max(0, 3 - length)

    order = sorted(range(len(usable)), key=lambda index: (-score(index), index))
    # The mood most candidates chose; in a tie, the best caption's.
    votes = Counter(mood for _, mood in usable)
    best_mood = usable[order[0]][1]
    mood = best_mood if votes[best_mood] == max(votes.values()) else votes.most_common(1)[0][0]
    return [usable[index][0] for index in order], mood


def with_emoji(caption: str, mood: str) -> str:
    return f"{caption} {MOOD_EMOJI.get(mood, MOOD_EMOJI['hype'])}".strip()


class AutoCaptioner:
    """Writes captions with the caption model, one clip at a time."""

    def __init__(self, model: CaptionModel):
        self.model = model
        self.server = LlamaServer(model)
        self._lock = threading.Lock()
        atexit.register(self.server.stop)
        # A server left running by a crash still holds its memory.
        self.server.stop_leftover()

    def write(self, frames: list[bytes], spoken: str, variation: int = 0) -> dict[str, object]:
        if not self.model.ready():
            raise CaptionModelError("Download the caption model first.")
        spoken = re.sub(r"\s+", " ", spoken or "").strip()[:_SPOKEN_LIMIT]
        # The speech engine often hears a streamer's "chat" as the name Chad.
        spoken = re.sub(r"\bChad\b", "chat", spoken)
        images = [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(frame).decode("ascii")}}
            for frame in frames
        ]
        with self._lock:
            described = self.server.chat({
                "messages": [{"role": "user", "content": [
                    *images, {"type": "text", "text": DESCRIBE_PROMPT.format(spoken=spoken or "(nothing)")},
                ]}],
                "max_tokens": 120, "temperature": 0.2, "seed": 7,
            })
            scene = str((described[0].get("message") or {}).get("content") or "").strip() if described else ""
            if not scene:
                raise CaptionModelError("The caption model could not describe this clip.")
            choices = self.server.chat({
                "messages": [
                    {"role": "system", "content": CAPTION_PROMPT},
                    {"role": "user", "content": f"Description of the clip: {scene}\nWords spoken in the clip: \"{spoken or '(nothing is said)'}\""},
                ],
                "n": _CANDIDATES, "max_tokens": 120, "temperature": 0.9, "seed": 7 + 101 * int(variation),
                "response_format": {"type": "json_schema", "json_schema": {"name": "caption", "schema": CAPTION_SCHEMA}},
            })
        candidates: list[tuple[str, str]] = []
        for choice in choices:
            try:
                payload = json.loads((choice.get("message") or {}).get("content") or "")
                candidates.append((str(payload.get("caption") or ""), str(payload.get("mood") or "")))
            except (ValueError, AttributeError):
                continue
        ranked, mood = rank_captions(candidates, spoken, scene)
        if not ranked:
            raise CaptionModelError("The caption model could not write a caption for this clip. Try New caption or type one.")
        return {
            "caption": with_emoji(ranked[0], mood),
            "alternatives": [with_emoji(caption, mood) for caption in ranked[1:]],
            "mood": mood,
            "scene": scene,
        }
