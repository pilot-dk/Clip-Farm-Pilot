from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .brand import APP_NAME, env
from .video import ffmpeg_executable, probe_video


SUPPORTED_DOMAINS = ("youtube.com", "youtu.be", "twitch.tv")
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
VIDEO_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def validate_vod_url(raw_url: str) -> str:
    url = raw_url.strip()
    if not url or len(url) > 2048:
        raise ValueError("Paste a valid YouTube or Twitch VOD link.")

    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    supported = any(hostname == domain or hostname.endswith(f".{domain}") for domain in SUPPORTED_DOMAINS)
    if parsed.scheme not in {"http", "https"} or not supported or parsed.username or parsed.password:
        raise ValueError("Clip Farm Pilot currently imports public YouTube and Twitch links.")
    return url


class _QuietLogger:
    def __init__(self, on_error):
        self.on_error = on_error

    def debug(self, _message):
        return None

    def info(self, _message):
        return None

    def warning(self, _message):
        return None

    def error(self, message):
        self.on_error(str(message))


class CachedVideoLibrary:
    """Persistent catalog for Clip Farm Pilot's local working video copies."""

    def __init__(self, uploads: Path, metadata_path: Path | None = None, trash_dir: Path | None = None):
        self.uploads = uploads.resolve()
        self.metadata_path = metadata_path or self.uploads.parent / "video_library.json"
        configured_trash = env("TRASH_DIR")
        self._use_system_trash = trash_dir is None and not configured_trash
        self.trash_dir = trash_dir or Path(configured_trash or self.uploads.parent / "trash").expanduser()
        self._records: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._load()
        self._discover_legacy_files()

    def _load(self) -> None:
        try:
            data = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = {}
        records = data.get("videos", {}) if isinstance(data, dict) else {}
        if isinstance(records, dict):
            self._records = {
                video_id: record
                for video_id, record in records.items()
                if VIDEO_ID_PATTERN.fullmatch(video_id) and isinstance(record, dict)
            }

    def _save_locked(self) -> None:
        self.metadata_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.metadata_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"version": 1, "videos": self._records}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, self.metadata_path)

    def _source_for(self, video_id: str) -> Path | None:
        if not VIDEO_ID_PATTERN.fullmatch(video_id or ""):
            return None
        candidates = [
            path.resolve()
            for path in self.uploads.glob(f"{video_id}.*")
            if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES
        ]
        candidates = [path for path in candidates if path.parent == self.uploads]
        return max(candidates, key=lambda path: path.stat().st_size) if candidates else None

    def _discover_legacy_files(self) -> None:
        changed = False
        with self._lock:
            for path in self.uploads.iterdir():
                if not path.is_file() or path.suffix.lower() not in VIDEO_SUFFIXES:
                    continue
                video_id = path.stem
                if not VIDEO_ID_PATTERN.fullmatch(video_id) or video_id in self._records:
                    continue
                self._records[video_id] = {
                    "video_id": video_id,
                    "title": f"Older cached video {video_id[:8]}",
                    "source_type": "legacy",
                    "original_url": "",
                    "created_at": path.stat().st_mtime,
                }
                changed = True
            if changed:
                self._save_locked()

    def register(
        self,
        video_id: str,
        title: str,
        source_type: str,
        original_url: str = "",
        duration: float | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        if not VIDEO_ID_PATTERN.fullmatch(video_id or ""):
            raise ValueError("Invalid video ID")
        with self._lock:
            self._records[video_id] = {
                "video_id": video_id,
                "title": str(title or "Cached video")[:240],
                "source_type": source_type,
                "original_url": original_url,
                "created_at": time.time(),
                "duration": duration,
                "width": width,
                "height": height,
            }
            self._save_locked()

    def list_items(self) -> list[dict]:
        items: list[dict] = []
        missing: list[str] = []
        with self._lock:
            for video_id, record in self._records.items():
                source = self._source_for(video_id)
                if source is None:
                    missing.append(video_id)
                    continue
                stat = source.stat()
                local_path = str(source)
                home = str(Path.home())
                display_path = f"~{local_path[len(home):]}" if local_path.startswith(home + os.sep) else local_path
                items.append({
                    **record,
                    "filename": source.name,
                    "size_bytes": stat.st_size,
                    "local_path": local_path,
                    "display_path": display_path,
                    "source_url": f"/api/videos/{video_id}/source",
                })
            if missing:
                for video_id in missing:
                    self._records.pop(video_id, None)
                self._save_locked()
        return sorted(items, key=lambda item: float(item.get("created_at") or 0), reverse=True)

    def source_path(self, video_id: str) -> Path:
        """Return a validated cached source path without accepting arbitrary paths."""
        if not VIDEO_ID_PATTERN.fullmatch(video_id or ""):
            raise KeyError(video_id)
        with self._lock:
            source = self._source_for(video_id)
            if video_id not in self._records or source is None:
                raise KeyError(video_id)
            return source

    def select_item(self, video_id: str) -> dict:
        """Return editor-ready metadata for a video selected from the library."""
        source = self.source_path(video_id)
        info = probe_video(source)
        with self._lock:
            record = self._records.get(video_id)
            if record is None:
                raise KeyError(video_id)
            record.update({"duration": round(info.duration, 2), "width": info.width, "height": info.height})
            self._save_locked()
        item = next((value for value in self.list_items() if value["video_id"] == video_id), None)
        if item is None:
            raise KeyError(video_id)
        return item

    def title_for(self, video_id: str) -> str:
        if not VIDEO_ID_PATTERN.fullmatch(video_id or ""):
            return ""
        with self._lock:
            record = self._records.get(video_id) or {}
            return str(record.get("title") or "")

    def move_to_trash(self, video_id: str) -> dict:
        if not VIDEO_ID_PATTERN.fullmatch(video_id or ""):
            raise KeyError(video_id)
        with self._lock:
            record = self._records.get(video_id)
            source = self._source_for(video_id)
            if not record or source is None:
                raise KeyError(video_id)

            permanent_deletion = str(env("DELETE_PERMANENT", "")).lower() in {"1", "true", "yes"}
            if permanent_deletion:
                source.unlink()
                disposition = "deleted"
            elif self._use_system_trash:
                from send2trash import send2trash

                send2trash(str(source))
                disposition = "trash"
            else:
                self.trash_dir.mkdir(parents=True, exist_ok=True)
                safe_title = re.sub(r"[^A-Za-z0-9 _.-]+", "", str(record.get("title") or "Imported video")).strip()
                safe_title = safe_title[:60] or "Imported video"
                destination = self.trash_dir / f"{APP_NAME} - {safe_title} - {video_id[:8]}{source.suffix.lower()}"
                counter = 2
                while destination.exists():
                    destination = self.trash_dir / f"{APP_NAME} - {safe_title} - {video_id[:8]} ({counter}){source.suffix.lower()}"
                    counter += 1
                shutil.move(str(source), str(destination))
                disposition = "trash"
            self._records.pop(video_id, None)
            self._save_locked()
            return {"video_id": video_id, "title": record.get("title") or "Imported video", "disposition": disposition}


class VodImportManager:
    def __init__(self, uploads: Path, library: CachedVideoLibrary):
        self.uploads = uploads
        self.library = library
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def create(self, raw_url: str) -> dict:
        url = validate_vod_url(raw_url)
        import_id = uuid.uuid4().hex
        video_id = uuid.uuid4().hex
        job = {
            "import_id": import_id,
            "video_id": video_id,
            "status": "queued",
            "progress": 0.0,
            "message": "Waiting to start…",
            "created_at": time.time(),
        }
        with self._lock:
            self._jobs[import_id] = job
        threading.Thread(target=self._download, args=(import_id, video_id, url), daemon=True).start()
        return self.get(import_id)

    def get(self, import_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(import_id)
            if not job:
                raise KeyError(import_id)
            return dict(job)

    def _update(self, import_id: str, **changes) -> None:
        with self._lock:
            if import_id in self._jobs:
                self._jobs[import_id].update(changes)

    def _cleanup(self, video_id: str) -> None:
        for path in self.uploads.glob(f"{video_id}.*"):
            if path.is_file():
                path.unlink(missing_ok=True)

    def _download(self, import_id: str, video_id: str, url: str) -> None:
        last_error = ""

        def remember_error(message: str) -> None:
            nonlocal last_error
            last_error = message

        def progress_hook(data: dict) -> None:
            status = data.get("status")
            if status == "downloading":
                downloaded = float(data.get("downloaded_bytes") or 0)
                total = float(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)
                progress = min(96.0, downloaded / total * 96.0) if total else 8.0
                speed = data.get("speed")
                speed_text = f" · {speed / 1_000_000:.1f} MB/s" if speed else ""
                self._update(
                    import_id,
                    status="downloading",
                    progress=round(progress, 1),
                    message=f"Downloading VOD{speed_text}",
                )
            elif status == "finished":
                self._update(import_id, status="processing", progress=97.0, message="Preparing video…")

        try:
            self._update(import_id, status="starting", progress=2.0, message="Reading VOD information…")
            output_template = str(self.uploads / f"{video_id}.%(ext)s")
            options = {
                "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/bv*[height<=1080]+ba/b[height<=1080]/b",
                "merge_output_format": "mp4",
                "outtmpl": output_template,
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "restrictfilenames": True,
                "socket_timeout": 30,
                "retries": 3,
                "fragment_retries": 3,
                "progress_hooks": [progress_hook],
                "logger": _QuietLogger(remember_error),
                "ffmpeg_location": ffmpeg_executable(),
            }
            node_runtime = env("NODE_EXE") or shutil.which("node")
            if node_runtime and Path(node_runtime).exists():
                options["js_runtimes"] = {"node": {"path": node_runtime}}
            with YoutubeDL(options) as ydl:
                details = ydl.extract_info(url, download=True)

            candidates = [
                path for path in self.uploads.glob(f"{video_id}.*")
                if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES
            ]
            if not candidates:
                raise RuntimeError("The VOD downloaded, but no usable video file was produced.")
            source = max(candidates, key=lambda path: path.stat().st_size)
            info = probe_video(source)
            title = str(details.get("title") or "Imported VOD")
            self.library.register(
                video_id=video_id,
                title=title,
                source_type="url",
                original_url=url,
                duration=round(info.duration, 2),
                width=info.width,
                height=info.height,
            )
            self._update(
                import_id,
                status="complete",
                progress=100.0,
                message="VOD ready",
                filename=f"{title}{source.suffix.lower()}",
                title=title,
                duration=round(info.duration, 2),
                width=info.width,
                height=info.height,
                source_url=f"/api/videos/{video_id}/source",
            )
        except (DownloadError, Exception) as exc:
            self._cleanup(video_id)
            message = last_error or str(exc) or "The VOD could not be imported."
            if "Sign in" in message or "login" in message.lower() or "private" in message.lower():
                message = "This VOD is private or requires a signed-in account. Try a public VOD."
            elif len(message) > 240:
                message = message[:237] + "…"
            self._update(import_id, status="error", progress=0.0, message=message)
