from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import io
import os
import secrets
import shutil
import tempfile
import time
import uuid
from html import escape
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field

from .brand import APP_NAME, APP_SLUG, APP_VERSION, env
from .social import SocialPublishError, SocialPublisher
from .video import (
    analyze_viral_candidates,
    center_caption_overlay,
    export_clip,
    generate_viral_title,
    probe_video,
)
from .vod import CachedVideoLibrary, VodImportManager

BASE = Path(__file__).resolve().parents[1]
STORAGE = Path(env("STORAGE_DIR", BASE / "storage")).expanduser()
UPLOADS = STORAGE / "uploads"
EXPORTS = STORAGE / "exports"
STATIC = Path(__file__).resolve().parent / "static"
UPLOADS.mkdir(parents=True, exist_ok=True)
EXPORTS.mkdir(parents=True, exist_ok=True)
VIDEO_LIBRARY = CachedVideoLibrary(UPLOADS)
VOD_IMPORTS = VodImportManager(UPLOADS, VIDEO_LIBRARY)
SOCIAL = SocialPublisher(STORAGE, EXPORTS)
WEB_PASSWORD = str(env("WEB_PASSWORD", ""))
SESSION_SECRET = str(env("SESSION_SECRET") or secrets.token_urlsafe(32))
SESSION_COOKIE = f"{APP_SLUG}_session"
SESSION_LIFETIME_SECONDS = 60 * 60 * 24 * 30

app = FastAPI(title=f"{APP_NAME} API", version=APP_VERSION)
CORS_ORIGINS = [value.strip() for value in str(env("CORS_ORIGINS", "")).split(",") if value.strip()]
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


class AnalyzeRequest(BaseModel):
    target_duration: int = Field(30, ge=8, le=90)
    limit: int = Field(5, ge=1, le=10)


class VodImportRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class ExportRequest(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    aspect: Literal["16:9", "9:16", "1:1"] = "9:16"
    layout: Literal["standard", "gaming"] = "standard"
    face_corner: Literal["top-left", "top-right", "bottom-left", "bottom-right"] = "top-right"
    face_width_fraction: float = Field(0.30, ge=0.10, le=0.70)
    face_height_fraction: float = Field(0.34, ge=0.10, le=0.70)
    face_inset_x_fraction: float = Field(0.02, ge=0.0, le=0.30)
    face_inset_y_fraction: float = Field(0.02, ge=0.0, le=0.30)
    caption_text: str = Field("", max_length=160)
    caption_font_scale: float = Field(1.0, ge=0.50, le=1.75)
    caption_position: Literal["top", "center", "bottom"] = "center"
    caption_overlay_data_url: str = Field("", max_length=3_000_000)
    sound_effect: Literal["none", "impact-boom", "vine-boom", "whoosh", "record-scratch"] = "none"
    visual_effect: Literal["none", "lens-flare", "punch-zoom", "white-flash"] = "none"
    effect_time: float = Field(1.0, ge=0.0, le=86_400.0)
    sound_volume: float = Field(1.0, ge=0.0, le=2.0)
    visual_strength: float = Field(1.0, ge=0.25, le=1.5)
    viral_title: bool = True


class SocialPublishRequest(BaseModel):
    export_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    platforms: list[Literal["youtube", "instagram", "tiktok"]] = Field(min_length=1, max_length=3)
    title: str = Field("", max_length=100)
    caption: str = Field("", max_length=2200)
    privacy: Literal["public", "unlisted", "private"] = "private"


class LoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=512)


def _session_signature(expires: int) -> str:
    message = f"{APP_SLUG}:{expires}".encode("utf-8")
    return hmac.new(SESSION_SECRET.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _authenticated(request: Request) -> bool:
    if not WEB_PASSWORD:
        return True
    token = request.cookies.get(SESSION_COOKIE, "")
    try:
        expires_text, supplied_signature = token.split(".", 1)
        expires = int(expires_text)
    except (TypeError, ValueError):
        return False
    return expires >= int(time.time()) and hmac.compare_digest(_session_signature(expires), supplied_signature)


def _secure_request(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    return request.url.scheme == "https" or forwarded_proto == "https"


@app.middleware("http")
async def protect_api(request: Request, call_next):
    public_api_paths = {"/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/logout"}
    if request.url.path.startswith("/api/") and request.url.path not in public_api_paths and not _authenticated(request):
        return JSONResponse({"detail": "Unlock Clip Farm Pilot to continue."}, status_code=401)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response


def _caption_overlay_from_data_url(
    value: str,
    caption_position: Literal["top", "center", "bottom"] = "center",
) -> Path | None:
    if not value:
        return None
    prefix = "data:image/png;base64,"
    if not value.startswith(prefix):
        raise ValueError("The square-caption image must be a PNG.")
    try:
        raw = base64.b64decode(value[len(prefix):], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("The square-caption image could not be read.") from exc
    if not raw or len(raw) > 2_000_000:
        raise ValueError("The square-caption image is too large.")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "PNG" or image.size != (1080, 1080):
                raise ValueError("The square-caption image must be a 1080 × 1080 PNG.")
            image.load()
            centered = center_caption_overlay(image, caption_position)
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("The square-caption image could not be read.") from exc
    temporary = tempfile.NamedTemporaryFile(prefix=f"{APP_SLUG}-browser-caption-", suffix=".png", delete=False)
    try:
        centered.save(temporary, format="PNG")
        return Path(temporary.name)
    finally:
        temporary.close()


def get_video(video_id: str) -> Path:
    matches = list(UPLOADS.glob(f"{video_id}.*"))
    if not matches:
        raise HTTPException(404, "Video not found")
    return matches[0]


@app.get("/api/health")
def health():
    return {"ok": True, "name": APP_NAME, "version": app.version}


@app.get("/api/auth/status")
def auth_status(request: Request):
    return {"required": bool(WEB_PASSWORD), "authenticated": _authenticated(request)}


@app.post("/api/auth/login")
def login(req: LoginRequest, request: Request, response: Response):
    if WEB_PASSWORD and not hmac.compare_digest(req.password, WEB_PASSWORD):
        raise HTTPException(401, "That password is not correct.")
    expires = int(time.time()) + SESSION_LIFETIME_SECONDS
    response.set_cookie(
        SESSION_COOKIE,
        f"{expires}.{_session_signature(expires)}",
        max_age=SESSION_LIFETIME_SECONDS,
        httponly=True,
        secure=_secure_request(request),
        samesite="lax",
        path="/",
    )
    return {"authenticated": True}


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/", samesite="lax")
    return {"authenticated": False}


@app.post("/api/upload")
def upload(video: UploadFile = File(...)):
    suffix = Path(video.filename or "video.mp4").suffix.lower() or ".mp4"
    if suffix not in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}:
        raise HTTPException(400, "Use MP4, MOV, MKV, WEBM, or M4V.")
    video_id = uuid.uuid4().hex
    target = UPLOADS / f"{video_id}{suffix}"
    with target.open("wb") as f:
        shutil.copyfileobj(video.file, f)
    try:
        info = probe_video(target)
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(400, f"Could not read the video: {exc}")
    VIDEO_LIBRARY.register(
        video_id=video_id,
        title=video.filename or target.name,
        source_type="upload",
        duration=round(info.duration, 2),
        width=info.width,
        height=info.height,
    )
    return {
        "video_id": video_id,
        "filename": video.filename,
        "duration": round(info.duration, 2),
        "width": info.width,
        "height": info.height,
    }


@app.post("/api/imports")
def create_vod_import(req: VodImportRequest):
    try:
        return VOD_IMPORTS.create(req.url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/imports/{import_id}")
def get_vod_import(import_id: str):
    try:
        return VOD_IMPORTS.get(import_id)
    except KeyError:
        raise HTTPException(404, "VOD import not found")


@app.get("/api/videos/{video_id}/source")
def get_video_source(video_id: str):
    source = get_video(video_id)
    return FileResponse(source, filename=source.name, content_disposition_type="inline")


@app.get("/api/library/videos")
def list_cached_videos():
    permanent_deletion = str(env("DELETE_PERMANENT", "")).lower() in {"1", "true", "yes"}
    return {
        "items": VIDEO_LIBRARY.list_items(),
        "storage_path": str(UPLOADS),
        "deletion_mode": "permanent" if permanent_deletion else "trash",
        "deletion_note": (
            "Source videos are permanently removed from this web server. Exported clips are not removed."
            if permanent_deletion else "Files are moved to Trash. Exported clips are not removed."
        ),
    }


@app.delete("/api/library/videos/{video_id}")
def delete_cached_video(video_id: str):
    try:
        return VIDEO_LIBRARY.move_to_trash(video_id)
    except KeyError:
        raise HTTPException(404, "Cached video not found")
    except OSError as exc:
        raise HTTPException(500, f"Could not move the video to Trash: {exc}")


@app.post("/api/videos/{video_id}/analyze")
def analyze(video_id: str, req: AnalyzeRequest):
    source = get_video(video_id)
    try:
        candidates = analyze_viral_candidates(source, req.target_duration, req.limit)
    except Exception as exc:
        raise HTTPException(500, f"Analysis failed: {exc}")
    return {"video_id": video_id, "candidates": candidates}


@app.post("/api/videos/{video_id}/export")
def export(video_id: str, req: ExportRequest):
    source = get_video(video_id)
    if req.end <= req.start:
        raise HTTPException(400, "End time must be after start time.")
    export_id = uuid.uuid4().hex
    target = EXPORTS / f"{export_id}.mp4"
    caption_overlay: Path | None = None
    try:
        if req.aspect == "1:1" and req.caption_text.strip() and req.caption_overlay_data_url:
            caption_overlay = _caption_overlay_from_data_url(req.caption_overlay_data_url, req.caption_position)
        export_clip(
            source=source,
            output=target,
            start=req.start,
            end=req.end,
            aspect=req.aspect,
            layout=req.layout,
            face_corner=req.face_corner,
            face_width_fraction=req.face_width_fraction,
            face_height_fraction=req.face_height_fraction,
            face_inset_x_fraction=req.face_inset_x_fraction,
            face_inset_y_fraction=req.face_inset_y_fraction,
            caption_text=req.caption_text,
            caption_font_scale=req.caption_font_scale,
            caption_position=req.caption_position,
            caption_overlay_path=caption_overlay,
            sound_effect=req.sound_effect,
            visual_effect=req.visual_effect,
            effect_time=req.effect_time,
            sound_volume=req.sound_volume,
            visual_strength=req.visual_strength,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(500, f"Export failed: {exc}")
    finally:
        if caption_overlay is not None:
            caption_overlay.unlink(missing_ok=True)
    fallback_filename = f"Clip Farm Pilot-{req.aspect.replace(':', 'x')}-{export_id[:8]}.mp4"
    title_result = {
        "title": Path(fallback_filename).stem,
        "filename": fallback_filename,
        "strategy": "standard",
    }
    if req.viral_title:
        try:
            title_result = generate_viral_title(
                clip_path=target,
                source_title=VIDEO_LIBRARY.title_for(video_id),
                caption_text=req.caption_text,
            )
        except Exception:
            # A title suggestion should never prevent a completed render from saving.
            pass
    return {
        "export_id": export_id,
        "download_url": f"/api/exports/{export_id}.mp4",
        "viral_title": title_result["title"],
        "suggested_filename": title_result["filename"],
        "title_strategy": title_result["strategy"],
    }


@app.get("/api/exports/{filename}")
def get_export(filename: str):
    target = EXPORTS / Path(filename).name
    if not target.exists():
        raise HTTPException(404, "Export not found")
    return FileResponse(target, media_type="video/mp4", filename=target.name)


@app.get("/api/social/accounts")
def social_accounts():
    return SOCIAL.statuses()


@app.post("/api/social/{platform}/connect")
def connect_social_account(platform: Literal["youtube", "instagram", "tiktok"], request: Request):
    try:
        return SOCIAL.start_connection(platform, str(request.base_url).rstrip("/"))
    except SocialPublishError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/social/{platform}/callback", response_class=HTMLResponse)
def complete_social_connection(
    platform: Literal["youtube", "instagram", "tiktok"],
    code: str = "",
    state: str = "",
    error: str = "",
    error_description: str = "",
):
    if error:
        message = error_description or error
        success = False
    else:
        try:
            result = SOCIAL.complete_connection(platform, code, state)
            message = f"{result['display_name']} is connected to Clip Farm Pilot."
            success = True
        except Exception as exc:
            message = str(exc) or "The account could not be connected."
            success = False
    color = "#b9f34a" if success else "#ff8e8e"
    heading = "Account connected" if success else "Connection failed"
    return HTMLResponse(f"""<!doctype html>
<html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\">
<title>Clip Farm Pilot — {escape(heading)}</title></head>
<body style=\"margin:0;min-height:100vh;display:grid;place-items:center;background:#090a0d;color:#f4f5f7;font:15px -apple-system,BlinkMacSystemFont,sans-serif\">
<main style=\"width:min(440px,calc(100% - 40px));padding:32px;border:1px solid #303540;border-radius:18px;background:#111318;text-align:center\">
<div style=\"width:48px;height:48px;display:grid;place-items:center;margin:0 auto 18px;border-radius:14px;background:{color}22;color:{color};font-size:24px\">{'✓' if success else '!'}</div>
<h1 style=\"margin:0 0 10px;font-size:22px\">{escape(heading)}</h1>
<p style=\"margin:0;color:#a2a7b2;line-height:1.55\">{escape(message)}</p>
<p style=\"margin:20px 0 0;color:#707684;font-size:12px\">You can close this browser tab and return to Clip Farm Pilot.</p>
</main></body></html>""")


@app.delete("/api/social/{platform}")
def disconnect_social_account(platform: Literal["youtube", "instagram", "tiktok"]):
    try:
        return SOCIAL.disconnect(platform)
    except SocialPublishError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/social/publish")
def publish_export(req: SocialPublishRequest):
    try:
        return SOCIAL.publish(req.export_id, req.platforms, req.title, req.caption, req.privacy)
    except SocialPublishError as exc:
        raise HTTPException(400, str(exc))


app.mount("/assets", StaticFiles(directory=STATIC), name="assets")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
