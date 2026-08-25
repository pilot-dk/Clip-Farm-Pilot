from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

from backend.app.brand import APP_NAME, ENV_PREFIX, env


DEFAULT_CLIP_FILENAME = f"{APP_NAME}-clip.mp4"

class DesktopApi:
    """Native helpers exposed only inside the packaged desktop window."""

    _ITEM_ID = re.compile(r"^[0-9a-f]{32}$")
    _VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
    _OAUTH_HOSTS = {
        "accounts.google.com",
        "developers.facebook.com",
        "developers.google.com",
        "developers.tiktok.com",
        "www.instagram.com",
        "www.tiktok.com",
    }

    def __init__(self, exports_dir: Path, uploads_dir: Path | None = None):
        self._exports_dir = exports_dir.resolve()
        self._uploads_dir = uploads_dir.resolve() if uploads_dir else None
        self._window = None
        self._save_dialog_type = 30

    def _bind_window(self, window, save_dialog_type: int) -> None:
        self._window = window
        self._save_dialog_type = save_dialog_type

    def save_export(self, export_id: str, suggested_name: str = DEFAULT_CLIP_FILENAME) -> dict:
        if not self._ITEM_ID.fullmatch(export_id or ""):
            raise ValueError("That exported clip could not be identified.")

        source = (self._exports_dir / f"{export_id}.mp4").resolve()
        if source.parent != self._exports_dir or not source.is_file():
            raise FileNotFoundError("The exported clip is no longer available. Please export it again.")
        if self._window is None:
            raise RuntimeError("The desktop save window is not ready.")

        safe_name = Path(suggested_name or DEFAULT_CLIP_FILENAME).name[:120]
        if not safe_name.lower().endswith(".mp4"):
            safe_name += ".mp4"
        downloads = Path.home() / "Downloads"
        initial_directory = str(downloads if downloads.is_dir() else Path.home())
        selected = self._window.create_file_dialog(
            self._save_dialog_type,
            directory=initial_directory,
            save_filename=safe_name,
            file_types=("MP4 video (*.mp4)",),
        )
        if not selected:
            return {"status": "cancelled"}

        destination_value = selected[0] if isinstance(selected, (tuple, list)) else selected
        destination = Path(destination_value).expanduser()
        if destination.suffix.lower() != ".mp4":
            destination = destination.with_suffix(".mp4")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.resolve() != source:
            shutil.copy2(source, destination)
        return {"status": "saved", "path": str(destination)}

    def reveal_video(self, video_id: str) -> dict:
        """Reveal one of Clip Farm Pilot's cached source videos in the system file manager."""
        if not self._ITEM_ID.fullmatch(video_id or ""):
            raise ValueError("That imported video could not be identified.")
        if self._uploads_dir is None:
            raise RuntimeError("The imported video folder is not available.")

        candidates = [
            path.resolve()
            for path in self._uploads_dir.glob(f"{video_id}.*")
            if path.is_file() and path.suffix.lower() in self._VIDEO_SUFFIXES
        ]
        candidates = [path for path in candidates if path.parent == self._uploads_dir]
        if not candidates:
            raise FileNotFoundError("The imported video is no longer on this computer.")
        source = max(candidates, key=lambda path: path.stat().st_size)
        if sys.platform == "darwin":
            command = ["open", "-R", str(source)]
        elif sys.platform == "win32":
            command = ["explorer", f"/select,{source}"]
        else:
            command = ["xdg-open", str(source.parent)]
        subprocess.run(command, check=True, capture_output=True)
        return {"status": "revealed", "path": str(source)}

    def open_external_url(self, url: str) -> dict:
        """Open only known OAuth or provider-help pages in the default browser."""
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in self._OAUTH_HOSTS:
            raise ValueError("Clip Farm Pilot blocked an unexpected external link.")
        if not webbrowser.open(url, new=2):
            raise RuntimeError("The sign-in page could not be opened in the default browser.")
        return {"status": "opened"}


def _resource_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _default_storage_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if sys.platform == "win32":
        local_data = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return Path(local_data) / APP_NAME if local_data else Path.home() / "AppData" / "Local" / APP_NAME
    data_home = os.environ.get("XDG_DATA_HOME")
    return (Path(data_home) if data_home else Path.home() / ".local" / "share") / "clipfarmpilot"


def _available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _configure_runtime() -> None:
    configured_storage = env("STORAGE_DIR")
    if configured_storage:
        storage = Path(str(configured_storage)).expanduser()
    else:
        storage = _default_storage_dir()
        legacy_storage = Path.home() / "Library" / "Application Support" / ("Clip" + "Pilot")
        if sys.platform == "darwin" and legacy_storage.is_dir() and not storage.exists():
            try:
                legacy_storage.rename(storage)
            except OSError:
                storage = legacy_storage
    storage.mkdir(parents=True, exist_ok=True)
    os.environ[f"{ENV_PREFIX}STORAGE_DIR"] = str(storage)

    import imageio_ffmpeg

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    os.environ["IMAGEIO_FFMPEG_EXE"] = ffmpeg
    os.environ[f"{ENV_PREFIX}FFMPEG_EXE"] = ffmpeg

    for node_name in ("node.exe", "node"):
        bundled_node = _resource_dir() / "bin" / node_name
        if bundled_node.exists():
            os.environ[f"{ENV_PREFIX}NODE_EXE"] = str(bundled_node)
            break


def _wait_for_server(url: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("Clip Farm Pilot could not start its local service.")


def _json_request(url: str, path: str, payload: dict | None = None, method: str | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        f"{url}{path}",
        data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method=method or ("POST" if data else "GET"),
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def _test_vod_pipeline(url: str, vod_url: str) -> list[str]:
    created = _json_request(url, "/api/imports", {"url": vod_url})
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        job = _json_request(url, f"/api/imports/{created['import_id']}")
        if job["status"] == "complete":
            library = _json_request(url, "/api/library/videos")
            cached = next((item for item in library["items"] if item["video_id"] == job["video_id"]), None)
            if not cached or cached.get("original_url") != vod_url:
                raise RuntimeError("The imported VOD was not added to the video library.")
            _json_request(url, f"/api/videos/{job['video_id']}/analyze", {"target_duration": 8, "limit": 1})
            clip_end = min(5, float(job["duration"]))
            gaming_export = _json_request(url, f"/api/videos/{job['video_id']}/export", {
                "start": 0,
                "end": clip_end,
                "aspect": "9:16",
                "layout": "gaming",
                "face_corner": "bottom-right",
                "face_width_fraction": 0.30,
                "face_height_fraction": 0.34,
                "face_inset_x_fraction": 0.02,
                "face_inset_y_fraction": 0.02,
                "video_filter": "cinematic",
            })
            square_export = _json_request(url, f"/api/videos/{job['video_id']}/export", {
                "start": 0,
                "end": clip_end,
                "aspect": "1:1",
                "layout": "standard",
                "caption_text": "W shave ❤️",
                "caption_font_scale": 1.50,
                "caption_position": "bottom",
                "video_filter": "warm",
            })
            if env("TEST_DELETE_LIBRARY") == "1":
                deleted = _json_request(url, f"/api/library/videos/{job['video_id']}", method="DELETE")
                if deleted.get("disposition") != "trash":
                    raise RuntimeError("The imported VOD was not moved to Trash.")
                remaining = _json_request(url, "/api/library/videos")
                if any(item["video_id"] == job["video_id"] for item in remaining["items"]):
                    raise RuntimeError("The deleted VOD still appears in the video library.")
            return [gaming_export["export_id"], square_export["export_id"]]
        if job["status"] == "error":
            raise RuntimeError(job["message"])
        time.sleep(0.5)
    raise RuntimeError("The bundled VOD import test timed out.")


def _test_save_bridge(
    exports_dir: Path,
    export_id: str,
    destination_dir: Path,
    suggested_name: str = f"{APP_NAME}-save-test.mp4",
) -> None:
    destination_dir.mkdir(parents=True, exist_ok=True)

    class TestWindow:
        def create_file_dialog(self, *args, **kwargs):
            return str(destination_dir / kwargs["save_filename"])

    desktop_api = DesktopApi(exports_dir)
    desktop_api._bind_window(TestWindow(), 30)
    result = desktop_api.save_export(export_id, suggested_name)
    destination = Path(result["path"])
    if result["status"] != "saved" or not destination.is_file():
        raise RuntimeError("The native save test did not create an MP4.")
    if destination.read_bytes() != (exports_dir / f"{export_id}.mp4").read_bytes():
        raise RuntimeError("The saved MP4 does not match the rendered export.")


def _test_direct_bundle(source_path: Path, uploads_dir: Path, exports_dir: Path, library, static_dir: Path) -> None:
    """Socket-free packaged-app test for restricted build environments."""
    from backend.app.captions import caption_engine_self_test, caption_engine_status
    from backend.app.video import analyze_viral_candidates, export_clip, generate_viral_title, probe_video

    if not source_path.is_file():
        raise FileNotFoundError("The direct bundle test source is missing.")
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    if (
        'id="libraryButton"' not in html
        or 'id="libraryModal"' not in html
        or 'id="viralTitleToggle"' not in html
        or 'id="publishButton"' not in html
        or 'id="soundEffect"' not in html
        or 'id="visualEffect"' not in html
        or 'id="effectTime"' not in html
        or 'id="autoSoundEffectToggle"' not in html
        or 'id="liveCaptionsToggle"' not in html
        or 'id="liveCaptionScheme"' not in html
    ):
        raise RuntimeError("The bundled Clip Farm Pilot interface is missing an expected feature.")
    if not caption_engine_status()["available"]:
        raise RuntimeError("The bundled offline live-caption engine is missing.")
    caption_engine_self_test()

    video_id = uuid.uuid4().hex
    cached_source = uploads_dir / f"{video_id}{source_path.suffix.lower()}"
    shutil.copy2(source_path, cached_source)
    info = probe_video(cached_source)
    library.register(
        video_id,
        "Bundled VOD test",
        "url",
        "https://www.youtube.com/watch?v=bundled-test",
        info.duration,
        info.width,
        info.height,
    )
    cached = next((item for item in library.list_items() if item["video_id"] == video_id), None)
    if not cached or cached.get("original_url") != "https://www.youtube.com/watch?v=bundled-test":
        raise RuntimeError("The bundled video library did not persist the VOD source.")

    candidates = analyze_viral_candidates(cached_source, target_duration=15, limit=3)
    if not candidates or not all(
        item.get("label") and item.get("reason") and item.get("signals")
        for item in candidates
    ):
        raise RuntimeError("The bundled multi-signal clip detector did not return explained candidates.")

    clip_end = min(2.5, info.duration)
    landscape_id = uuid.uuid4().hex
    portrait_id = uuid.uuid4().hex
    square_id = uuid.uuid4().hex
    gaming_id = uuid.uuid4().hex
    portrait_metadata: dict[str, object] = {}
    landscape_sound_times = export_clip(
        source=cached_source,
        output=exports_dir / f"{landscape_id}.mp4",
        start=0,
        end=clip_end,
        aspect="16:9",
        video_filter="cinematic",
        sound_effect="record-scratch",
        visual_effect="white-flash",
        effect_time=0.6,
        auto_sound_effect=False,
    )
    portrait_sound_times = export_clip(
        source=cached_source,
        output=exports_dir / f"{portrait_id}.mp4",
        start=0,
        end=clip_end,
        aspect="9:16",
        video_filter="black-white",
        sound_effect="impact-boom",
        visual_effect="punch-zoom",
        effect_time=0.6,
        live_captions=True,
        live_caption_scheme="neon-pink",
        title_transcript=True,
        export_metadata=portrait_metadata,
    )
    square_sound_times = export_clip(
        source=cached_source,
        output=exports_dir / f"{square_id}.mp4",
        start=0,
        end=clip_end,
        aspect="1:1",
        caption_text="W shave ❤️",
        caption_font_scale=1.50,
        caption_position="bottom",
        video_filter="warm",
        sound_effect="vine-boom",
        visual_effect="lens-flare",
        effect_time=0.6,
    )
    gaming_sound_times = export_clip(
        source=cached_source,
        output=exports_dir / f"{gaming_id}.mp4",
        start=0,
        end=clip_end,
        aspect="9:16",
        layout="gaming",
        face_corner="bottom-right",
        video_filter="vivid",
        sound_effect="impact-boom",
        visual_effect="lens-flare",
        effect_time=0.6,
    )
    if landscape_sound_times != [0.6] or not all(
        times for times in (portrait_sound_times, square_sound_times, gaming_sound_times)
    ):
        raise RuntimeError("The bundled smart/manual sound placement test did not return timestamps.")
    title_result = generate_viral_title(
        exports_dir / f"{gaming_id}.mp4",
        source_title="FC 26 Weekend League Livestream.mp4",
        transcript_text=str(portrait_metadata.get("title_transcript", "")),
        variation_seed=gaming_id,
    )
    if not title_result["title"] or title_result["filename"].startswith(f"{APP_NAME}-"):
        raise RuntimeError("The bundled viral filename generator did not produce a content-aware title.")
    library.move_to_trash(video_id)
    expected_exports = [landscape_id, portrait_id, square_id, gaming_id]
    if cached_source.exists() or any(not (exports_dir / f"{item}.mp4").is_file() for item in expected_exports):
        raise RuntimeError("Deleting the bundled test VOD did not preserve its exports.")
    if any(item["video_id"] == video_id for item in library.list_items()):
        raise RuntimeError("The deleted bundled test VOD remains in the video library.")
    if save_dir := env("TEST_SAVE_DIR"):
        _test_save_bridge(exports_dir, gaming_id, Path(save_dir), title_result["filename"])


def main() -> int:
    _configure_runtime()

    import uvicorn
    from backend.app.main import EXPORTS, STATIC, UPLOADS, VIDEO_LIBRARY, app

    if direct_source := env("TEST_SOURCE"):
        _test_direct_bundle(Path(str(direct_source)), UPLOADS, EXPORTS, VIDEO_LIBRARY, STATIC)
        return 0

    port = _available_port()
    url = f"http://127.0.0.1:{port}"
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    thread = threading.Thread(target=server.run, name=f"{APP_NAME} API", daemon=True)
    thread.start()

    try:
        _wait_for_server(url)
        if env("TEST_MODE") == "1":
            from backend.app.captions import caption_engine_self_test

            health = _json_request(url, "/api/health")
            if not health.get("live_captions", {}).get("available"):
                raise RuntimeError("The packaged offline live-caption engine is missing.")
            caption_engine_self_test()
            export_ids: list[str] = []
            if test_vod_url := env("TEST_VOD_URL"):
                export_ids = _test_vod_pipeline(url, str(test_vod_url))
            if export_ids and (test_save_dir := env("TEST_SAVE_DIR")):
                _test_save_bridge(EXPORTS, export_ids[0], Path(str(test_save_dir)))
            return 0

        import webview

        desktop_api = DesktopApi(EXPORTS, UPLOADS)
        window = webview.create_window(
            APP_NAME,
            url,
            js_api=desktop_api,
            width=1360,
            height=900,
            min_size=(980, 650),
            background_color="#090a0d",
        )
        desktop_api._bind_window(window, webview.FileDialog.SAVE)
        def close_test_window():
            time.sleep(2)
            window.destroy()

        test_window = env("TEST_WINDOW") == "1"
        desktop_gui = "cocoa" if sys.platform == "darwin" else "edgechromium" if sys.platform == "win32" else "gtk"
        webview.start(close_test_window if test_window else None, gui=desktop_gui, debug=False, private_mode=False)
        return 0
    finally:
        server.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
