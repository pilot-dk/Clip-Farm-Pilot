from __future__ import annotations

import hashlib
import http.server
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from PIL import Image

from backend.app import autocaption, main
from backend.app.autocaption import (
    AutoCaptioner,
    CaptionModel,
    CaptionModelError,
    ModelFile,
    caption_is_usable,
    clean_caption,
    clip_frames,
    rank_captions,
    with_emoji,
)
from backend.app.video import ffmpeg_executable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import prepare_caption_runtime  # noqa: E402


class CaptionTextTests(unittest.TestCase):
    def test_a_caption_loses_quotes_emojis_hashtags_and_its_full_stop(self):
        self.assertEqual(clean_caption('"bro shows off his rescued puppy." 🥹❤️‍🩹 #dogs'), "Bro shows off his rescued puppy")
        self.assertEqual(clean_caption("Bro is NOT ready!"), "Bro is NOT ready!")
        self.assertEqual(clean_caption("   "), "")

    def test_a_caption_must_read_like_a_fan_page_and_stay_within_the_clip(self):
        spoken = "Jason wake up, all of this is your fault. My favorite snack would be Nerds."
        scene = "A man in a Superman costume runs onto a film set."
        for caption in ("Bro shows off his rescued puppy", "Jason wakes up on set", "Bro loves cluster Nerds",
                        "Bro runs onto set in his Superman suit", "Bro is NOT a morning person"):
            self.assertTrue(caption_is_usable(caption, spoken, scene), caption)
        for caption in (
            "I got the puppy",                          # The creator's words, not the page's.
            "Bro",                                      # Too short to say anything.
            "Bro walks and walks and walks and walks around the park",  # Too long.
            "Bro yells in this clip",                   # Talks about the video itself.
            "Bro is the fucking dog",                   # Swears.
            "She yells at Bro to stay put",             # "Bro" used as a name.
            "Bro and She are about to climb",
            "Riz Ahmed inspires small streamers",       # A name nobody said.
        ):
            self.assertFalse(caption_is_usable(caption, spoken, scene), caption)

    def test_the_caption_the_others_agree_with_wins_and_one_misreading_loses(self):
        candidates = [
            ("Bro takes his dog on a hike", "wholesome"),     # "dog" was slang.
            ("Bro is ready to climb the mountain", "hype"),
            ("He prepares to climb the mountain tomorrow", "hype"),
            ("Bro stares at the mountain before the climb", "hype"),
            ("bro is ready to climb the mountain.", "hype"),  # The same caption again.
            ("I am about to climb", "hype"),                   # Not usable.
        ]
        ranked, mood = rank_captions(candidates, "We are really about to climb. That's it tomorrow, dog.")
        self.assertEqual(ranked[0], "Bro is ready to climb the mountain")
        self.assertEqual(ranked[-1], "Bro takes his dog on a hike")
        self.assertEqual(len(ranked), 4)
        self.assertEqual(mood, "hype")
        self.assertEqual(rank_captions([("I did it", "win")], ""), ([], ""))

    def test_a_tied_mood_follows_the_best_caption(self):
        ranked, mood = rank_captions([("Bro saves a puppy", "wholesome"), ("Bro saves a tiny puppy", "funny")], "puppy")
        self.assertEqual(mood, {"Bro saves a puppy": "wholesome", "Bro saves a tiny puppy": "funny"}[ranked[0]])

    def test_the_mood_picks_the_emojis(self):
        self.assertEqual(with_emoji("Bro introduces his new puppy", "wholesome"), "Bro introduces his new puppy 🥹❤️‍🩹")
        self.assertEqual(with_emoji("Bro is NOT made for the wildlife", "funny"), "Bro is NOT made for the wildlife 😭💔🥀")
        self.assertEqual(with_emoji("They win the whole thing", "win"), "They win the whole thing 🏆🔥")
        self.assertEqual(with_emoji("Bro hits a crazy shot", "unknown"), "Bro hits a crazy shot 🔥")


class _FileServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


def serve_files(files: dict[str, bytes], honour_ranges: bool = True):
    """A local HTTP server for the model files; it records each request's Range header."""
    requests: list[tuple[str, str | None]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            name = self.path.rsplit("/", 1)[-1]
            body = files.get(name)
            requests.append((name, self.headers.get("Range")))
            if body is None:
                self.send_error(404)
                return
            start = 0
            if honour_ranges and self.headers.get("Range", "").startswith("bytes="):
                start = int(self.headers["Range"][6:].split("-")[0])
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
            else:
                self.send_response(200)
            self.send_header("Content-Length", str(len(body) - start))
            self.end_headers()
            self.wfile.write(body[start:])

    server = _FileServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/resolve/abc", requests


def wait_for(model: CaptionModel, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while model.downloading() and time.monotonic() < deadline:
        time.sleep(0.02)
    return model.status()


class CaptionModelDownloadTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.root = Path(self._folder.name)
        self.weights = os.urandom(3 * 1024 * 1024 + 123)
        self.projector = os.urandom(700_000)
        self.files = (
            ModelFile("model.gguf", len(self.weights), hashlib.sha256(self.weights).hexdigest()),
            ModelFile("mmproj.gguf", len(self.projector), hashlib.sha256(self.projector).hexdigest()),
        )

    def tearDown(self):
        self._folder.cleanup()

    def model(self, url: str) -> CaptionModel:
        return CaptionModel(self.root / "models", url, self.files)

    def test_the_model_downloads_once_and_is_verified(self):
        server, url, requests = serve_files({"model.gguf": self.weights, "mmproj.gguf": self.projector})
        try:
            model = self.model(url)
            self.assertEqual(model.status()["state"], "missing")
            model.start_download()
            status = wait_for(model)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["downloaded_bytes"], status["total_bytes"])
        self.assertEqual((self.root / "models" / "model.gguf").read_bytes(), self.weights)
        self.assertTrue(model.ready())
        self.assertEqual([name for name, _ in requests], ["model.gguf", "mmproj.gguf"])
        # Already there: nothing more is fetched.
        model.start_download()
        self.assertEqual(wait_for(model)["state"], "ready")
        self.assertEqual(len(requests), 2)

    def test_an_interrupted_download_resumes_where_it_stopped(self):
        folder = self.root / "models"
        folder.mkdir()
        (folder / "model.part").write_bytes(self.weights[:1_000_000])
        server, url, requests = serve_files({"model.gguf": self.weights, "mmproj.gguf": self.projector})
        try:
            model = self.model(url)
            model.start_download()
            status = wait_for(model)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(status["state"], "ready")
        self.assertIn(("model.gguf", "bytes=1000000-"), requests)
        self.assertEqual((folder / "model.gguf").read_bytes(), self.weights)

    def test_a_server_that_ignores_the_resume_request_starts_the_file_over(self):
        folder = self.root / "models"
        folder.mkdir()
        (folder / "model.part").write_bytes(self.weights[:1_000_000])
        server, url, _ = serve_files({"model.gguf": self.weights, "mmproj.gguf": self.projector}, honour_ranges=False)
        try:
            model = self.model(url)
            model.start_download()
            status = wait_for(model)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(status["state"], "ready")
        self.assertEqual((folder / "model.gguf").read_bytes(), self.weights)

    def test_a_damaged_download_is_thrown_away(self):
        damaged = bytearray(self.weights)
        damaged[1234] ^= 0xFF
        server, url, _ = serve_files({"model.gguf": bytes(damaged), "mmproj.gguf": self.projector})
        try:
            model = self.model(url)
            model.start_download()
            status = wait_for(model)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(status["state"], "error")
        self.assertIn("damaged", status["error"])
        self.assertFalse(model.ready())
        self.assertFalse((self.root / "models" / "model.gguf").exists())
        self.assertFalse((self.root / "models" / "model.part").exists())

    def test_too_little_disk_space_is_reported_before_downloading(self):
        server, url, requests = serve_files({"model.gguf": self.weights, "mmproj.gguf": self.projector})
        try:
            model = self.model(url)
            with patch.object(autocaption.shutil, "disk_usage", return_value=type("Usage", (), {"free": 1_000_000})()):
                model.start_download()
                status = wait_for(model)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(status["state"], "error")
        self.assertIn("free disk space", status["error"])
        self.assertEqual(requests, [])

    def test_a_cancelled_download_pauses_and_keeps_what_it_has(self):
        folder = self.root / "models"
        folder.mkdir()
        (folder / "model.part").write_bytes(self.weights[:1_000_000])
        server, url, _ = serve_files({"model.gguf": self.weights, "mmproj.gguf": self.projector})
        try:
            model = self.model(url)
            model._cancel.set()  # Cancelled before the next chunk lands.
            thread = threading.Thread(target=model._download_all)
            thread.start()
            thread.join(10)
            status = model.status()
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(status["state"], "missing")
        self.assertEqual(status["error"], "")
        self.assertEqual(status["downloaded_bytes"], 1_000_000)
        self.assertFalse(model.ready())
        # A fresh start of the app still knows how far it got.
        self.assertEqual(self.model(url).status()["downloaded_bytes"], 1_000_000)


FAKE_SERVER = r'''
import http.server, json, sys
args = sys.argv[1:]
port = int(args[args.index("--port") + 1])
key = args[args.index("--api-key") + 1]
log = open(sys.argv[0] + ".requests", "a")
CAPTIONS = ["Bro shows off his rescued puppy", "Bro proudly shows his rescued puppy", "I got the puppy",
            "Bro shares his heartwarming puppy rescue story", "Bro is the fucking dog", "Bro shows off his rescued puppy."]
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        self.send_response(200 if self.path == "/health" else 404); self.end_headers(); self.wfile.write(b"{}")
    def do_POST(self):
        if self.headers.get("Authorization") != f"Bearer {key}":
            self.send_response(401); self.end_headers(); return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        log.write(json.dumps(body) + "\n"); log.flush()
        if "response_format" in body:
            choices = [{"message": {"content": json.dumps({"moment": "puppy", "mood": "wholesome", "caption": text})}}
                       for text in CAPTIONS[: body.get("n", 1)]]
        else:
            choices = [{"message": {"content": "A man in a cap shows a small puppy lying on the grass."}}]
        data = json.dumps({"choices": choices}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''


class AutoCaptionerTests(unittest.TestCase):
    def setUp(self):
        self._folder = tempfile.TemporaryDirectory()
        self.root = Path(self._folder.name)
        self.server_script = self.root / "llama-server"
        self.server_script.write_text(f"#!{sys.executable}\n{FAKE_SERVER}", encoding="utf-8")
        self.server_script.chmod(self.server_script.stat().st_mode | stat.S_IEXEC)
        self.model = CaptionModel(self.root / "models", "http://unused", (ModelFile("m.gguf", 3, "a" * 64), ModelFile("p.gguf", 3, "b" * 64)))
        self.model.directory.mkdir()
        for item in self.model.files:
            self.model.path(item).write_bytes(b"abc")
        (self.model.directory / "verified.json").write_text(json.dumps({item.name: item.sha256 for item in self.model.files}))
        self.environment = patch.dict(os.environ, {"CLIPFARMPILOT_LLAMA_SERVER": str(self.server_script)})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self._folder.cleanup()

    def requests(self) -> list[dict]:
        path = Path(str(self.server_script) + ".requests")
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    @unittest.skipIf(os.name == "nt", "the stand-in server is a script")
    def test_a_caption_is_written_from_the_frames_and_the_words(self):
        captioner = AutoCaptioner(self.model)
        try:
            result = captioner.write([b"one", b"two", b"three", b"four"], "I got the puppy. He is currently my son bro.")
            first_pid = captioner.server._process.pid
            again = captioner.write([b"one"], "puppy", variation=2)
            self.assertEqual(captioner.server._process.pid, first_pid)  # The model stays loaded.
        finally:
            captioner.server.stop()
        self.assertEqual(result["caption"], "Bro shows off his rescued puppy 🥹❤️‍🩹")
        self.assertEqual(result["alternatives"], ["Bro proudly shows his rescued puppy 🥹❤️‍🩹", "Bro shares his heartwarming puppy rescue story 🥹❤️‍🩹"])
        self.assertEqual(result["mood"], "wholesome")
        self.assertEqual(again["caption"], result["caption"])
        describe, write, _, rewrite = self.requests()
        images = [part for part in describe["messages"][0]["content"] if part["type"] == "image_url"]
        self.assertEqual(len(images), 4)
        self.assertTrue(images[0]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertIn("I got the puppy", describe["messages"][0]["content"][-1]["text"])
        self.assertEqual(describe["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(write["n"], 6)
        self.assertIn("A man in a cap shows a small puppy", write["messages"][1]["content"])
        self.assertNotEqual(write["seed"], rewrite["seed"])
        self.assertFalse(captioner.server.running())
        self.assertFalse((self.model.directory / "llama-server.pid").exists())

    @unittest.skipIf(os.name == "nt", "the stand-in server is a script")
    def test_a_server_left_behind_by_a_crash_is_stopped(self):
        leftover = subprocess.Popen([str(self.server_script), "--port", "0", "--api-key", "x"])
        try:
            (self.model.directory / "llama-server.pid").write_text(str(leftover.pid))
            with patch.object(autocaption, "_process_running", return_value=True):
                AutoCaptioner(self.model)
            self.assertIsNotNone(leftover.wait(timeout=10))
        finally:
            if leftover.poll() is None:
                leftover.kill()
        self.assertFalse((self.model.directory / "llama-server.pid").exists())

    def test_without_the_model_or_the_engine_nothing_is_written(self):
        missing = CaptionModel(self.root / "elsewhere", "http://unused", self.model.files)
        with self.assertRaisesRegex(CaptionModelError, "Download the caption model first"):
            AutoCaptioner(missing).write([b"x"], "")
        with patch.dict(os.environ, {"CLIPFARMPILOT_LLAMA_SERVER": str(self.root / "nothing-here")}):
            with self.assertRaisesRegex(CaptionModelError, "caption engine is missing"):
                AutoCaptioner(self.model).server.chat({})

    @unittest.skipIf(os.name == "nt", "the stand-in server is a script")
    def test_an_engine_that_cannot_start_says_why(self):
        self.server_script.write_text(f"#!{sys.executable}\nimport sys\nprint('failed to load model: out of memory')\nsys.exit(1)\n")
        with self.assertRaisesRegex(CaptionModelError, "out of memory"):
            AutoCaptioner(self.model).server.chat({})


class EngineSelfTestTests(unittest.TestCase):
    def test_the_engine_starts_with_its_own_folder_first_on_the_library_path(self):
        # A packaged app's own libraries can share names with llama.cpp's.
        with tempfile.TemporaryDirectory() as temporary:
            server = Path(temporary) / "llm" / "llama-server"
            server.parent.mkdir()
            server.write_text("")
            finished = subprocess.CompletedProcess([], 0, stdout="version: 0.7.0-dev (build 11799)\n", stderr="")
            with patch.dict(os.environ, {"CLIPFARMPILOT_LLAMA_SERVER": str(server)}), \
                    patch.object(autocaption.subprocess, "run", return_value=finished) as run:
                self.assertEqual(autocaption.engine_self_test(), "version: 0.7.0-dev (build 11799)")
            variable = "PATH" if os.name == "nt" else "DYLD_LIBRARY_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH"
            self.assertTrue(run.call_args.kwargs["env"][variable].startswith(str(server.parent) + os.pathsep))
            failed = subprocess.CompletedProcess([], 1, stdout="", stderr="dyld: Symbol not found")
            with patch.dict(os.environ, {"CLIPFARMPILOT_LLAMA_SERVER": str(server)}), \
                    patch.object(autocaption.subprocess, "run", return_value=failed), \
                    self.assertRaisesRegex(CaptionModelError, "startup test"):
                autocaption.engine_self_test()


class ClipFrameTests(unittest.TestCase):
    def test_four_frames_come_from_across_the_clip_fitted_to_448_pixels(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "wide.mp4"
            subprocess.run([
                ffmpeg_executable(), "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=8",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
            ], check=True)
            frames = clip_frames(source, 2.0, 6.0, ffmpeg_executable())
            self.assertEqual(len(frames), 4)
            for frame in frames:
                with Image.open(__import__("io").BytesIO(frame)) as image:
                    self.assertEqual(image.format, "JPEG")
                    self.assertEqual(image.size, (448, 252))
            with self.assertRaises(CaptionModelError):
                clip_frames(Path(temporary) / "missing.mp4", 0, 4, ffmpeg_executable())


class AutoCaptionApiTests(unittest.TestCase):
    def test_status_says_why_auto_captions_are_unavailable(self):
        with patch.object(main, "engine_available", return_value=False):
            status = main.auto_caption_status()
        self.assertFalse(status["available"])
        self.assertIn("Reinstall", status["reason"])
        with patch.object(main, "AUTO_CAPTIONS_ENABLED", False):
            status = main.auto_caption_status()
        self.assertFalse(status["available"])
        self.assertIn("desktop app", status["reason"])
        with patch.object(main, "AUTO_CAPTIONS_ENABLED", False), self.assertRaises(HTTPException) as raised:
            main.download_caption_model()
        self.assertEqual(raised.exception.status_code, 409)

    def test_a_caption_needs_the_model_and_comes_back_for_the_clip(self):
        request = main.AutoCaptionRequest(start=2, end=12)
        with patch.object(main, "get_video", return_value=Path("clip.mp4")), \
                patch.object(main, "engine_available", return_value=True), \
                patch.object(main.CAPTION_MODEL, "status", return_value={"state": "missing"}), \
                self.assertRaises(HTTPException) as raised:
            main.write_auto_caption("a" * 32, request)
        self.assertEqual(raised.exception.status_code, 409)

        written = {"caption": "Bro shows off his rescued puppy 🥹❤️‍🩹", "alternatives": [], "mood": "wholesome", "scene": ""}
        with patch.object(main, "get_video", return_value=Path("clip.mp4")), \
                patch.object(main, "engine_available", return_value=True), \
                patch.object(main.CAPTION_MODEL, "status", return_value={"state": "ready"}), \
                patch.object(main, "probe_video", return_value=type("Info", (), {"duration": 60.0})()), \
                patch.object(main, "_transcribe_words_cached", return_value=[]), \
                patch.object(main, "clip_frames", return_value=[b"frame"]) as frames, \
                patch.object(main.AUTO_CAPTIONER, "write", return_value=written) as write:
            self.assertEqual(main.write_auto_caption("a" * 32, request), written)
        frames.assert_called_once()
        write.assert_called_once_with([b"frame"], "", 0)

    def test_exports_take_a_caption_style(self):
        self.assertEqual(main.ExportRequest(start=0, end=10).caption_style, "bold")
        self.assertEqual(main.ExportRequest(start=0, end=10, caption_style="clean").caption_style, "clean")
        with self.assertRaises(ValueError):
            main.ExportRequest(start=0, end=10, caption_style="neon")


class EngineBundleTests(unittest.TestCase):
    def test_only_the_server_its_libraries_and_licences_are_bundled(self):
        keep = prepare_caption_runtime.keep_llama_file
        for name in ("llama-server", "llama-server.exe", "libllama-server-impl.dylib", "llama-server-impl.dll",
                     "libggml-base.0.27.0.dylib", "libggml-cpu-haswell.so", "libmtmd.so.0", "ggml-cpu-x64.dll",
                     "libomp.dll", "LICENSE", "LICENSE-LLVM-OpenMP"):
            self.assertTrue(keep(name), name)
        for name in ("llama-cli", "llama-mtmd-cli.exe", "libllama-cli-impl.dylib", "llama-bench-impl.dll", "README.md"):
            self.assertFalse(keep(name), name)
        self.assertEqual(
            set(prepare_caption_runtime.LLAMA_ARCHIVES),
            {("macos", "arm64"), ("linux", "x64"), ("linux", "arm64"), ("windows", "x64"), ("windows", "arm64")},
        )


if __name__ == "__main__":
    unittest.main()
