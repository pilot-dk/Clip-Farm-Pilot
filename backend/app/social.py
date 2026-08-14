from __future__ import annotations

import json
import math
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

import requests

from .brand import env


Platform = Literal["youtube", "instagram", "tiktok"]


class SocialPublishError(RuntimeError):
    """A user-readable error returned by an official social platform API."""


PLATFORM_DETAILS: dict[str, dict[str, str]] = {
    "youtube": {
        "name": "YouTube",
        "destination": "YouTube / Shorts",
        "docs_url": "https://developers.google.com/youtube/v3/guides/uploading_a_video",
        "account_note": "Any YouTube channel",
    },
    "instagram": {
        "name": "Instagram",
        "destination": "Instagram Reels",
        "docs_url": "https://developers.facebook.com/docs/instagram-platform/content-publishing/",
        "account_note": "Creator or Business account required",
    },
    "tiktok": {
        "name": "TikTok",
        "destination": "TikTok",
        "docs_url": "https://developers.tiktok.com/products/content-posting-api/",
        "account_note": "Content Posting API approval required",
    },
}


class SocialPublisher:
    """OAuth account storage and official-API uploads for exported clips.

    Provider app credentials are deliberately supplied through environment
    variables instead of the browser UI. This prevents secrets from being
    returned to JavaScript or written into the project bundle by accident.
    """

    def __init__(self, storage_dir: Path, exports_dir: Path, session: Any | None = None):
        self.storage_dir = Path(storage_dir)
        self.exports_dir = Path(exports_dir)
        self.accounts_path = self.storage_dir / "social-accounts.json"
        self.session = session or requests.Session()
        self._lock = threading.RLock()
        self._oauth_states: dict[str, dict[str, Any]] = {}

    def _credentials(self, platform: Platform) -> dict[str, str]:
        if platform == "youtube":
            return {
                "client_id": str(env("YOUTUBE_CLIENT_ID", "")).strip(),
                "client_secret": str(env("YOUTUBE_CLIENT_SECRET", "")).strip(),
            }
        if platform == "instagram":
            return {
                "client_id": str(env("INSTAGRAM_CLIENT_ID", "")).strip(),
                "client_secret": str(env("INSTAGRAM_CLIENT_SECRET", "")).strip(),
            }
        if platform == "tiktok":
            return {
                "client_id": str(env("TIKTOK_CLIENT_KEY", "")).strip(),
                "client_secret": str(env("TIKTOK_CLIENT_SECRET", "")).strip(),
            }
        raise SocialPublishError("Unsupported social platform.")

    def _read_accounts(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            try:
                payload = json.loads(self.accounts_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                return {}
            return payload if isinstance(payload, dict) else {}

    def _write_accounts(self, accounts: dict[str, dict[str, Any]]) -> None:
        with self._lock:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
            temporary = self.accounts_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(accounts, indent=2), encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.accounts_path)

    def _environment_account(self, platform: Platform) -> dict[str, Any] | None:
        platform_prefix = platform.upper()
        token = str(env(f"{platform_prefix}_ACCESS_TOKEN", "")).strip()
        if not token:
            return None
        account: dict[str, Any] = {
            "access_token": token,
            "display_name": env(f"{platform_prefix}_ACCOUNT_NAME", PLATFORM_DETAILS[platform]["name"]),
            "source": "environment",
        }
        if platform == "instagram":
            account["user_id"] = str(env("INSTAGRAM_USER_ID", "")).strip()
        if platform == "tiktok":
            account["open_id"] = str(env("TIKTOK_OPEN_ID", "")).strip()
        return account

    def _account(self, platform: Platform) -> dict[str, Any] | None:
        return self._environment_account(platform) or self._read_accounts().get(platform)

    def statuses(self) -> dict[str, Any]:
        accounts = self._read_accounts()
        result = []
        for platform, details in PLATFORM_DETAILS.items():
            typed_platform = platform  # keeps the dictionary order stable in the UI
            stored = self._environment_account(typed_platform) or accounts.get(platform)
            credentials = self._credentials(typed_platform)
            configured = bool(credentials["client_id"] and credentials["client_secret"])
            connected = bool(stored and stored.get("access_token"))
            result.append({
                "platform": platform,
                **details,
                "configured": configured or connected,
                "connected": connected,
                "display_name": stored.get("display_name") if stored else None,
                "expires_at": stored.get("expires_at") if stored else None,
            })
        return {
            "items": result,
            "notice": "Direct publishing uses each platform's official sign-in and posting API.",
        }

    def start_connection(self, platform: Platform, redirect_base: str) -> dict[str, Any]:
        details = PLATFORM_DETAILS[platform]
        credentials = self._credentials(platform)
        if not credentials["client_id"] or not credentials["client_secret"]:
            return {
                "status": "setup_required",
                "platform": platform,
                "message": f"{details['name']} developer credentials have not been added to this build yet.",
                "docs_url": details["docs_url"],
            }

        configured_base = str(env("OAUTH_REDIRECT_BASE", "")).strip().rstrip("/")
        base = configured_base or redirect_base.rstrip("/")
        redirect_uri = f"{base}/api/social/{platform}/callback"
        state = secrets.token_urlsafe(32)
        self._oauth_states[state] = {
            "platform": platform,
            "redirect_uri": redirect_uri,
            "expires": time.time() + 600,
        }

        if platform == "youtube":
            query = {
                "client_id": credentials["client_id"],
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "https://www.googleapis.com/auth/youtube.upload",
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
            }
            auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(query)}"
        elif platform == "instagram":
            query = {
                "client_id": credentials["client_id"],
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "instagram_business_basic,instagram_business_content_publish",
                "state": state,
                "enable_fb_login": "0",
                "force_authentication": "1",
            }
            auth_url = f"https://www.instagram.com/oauth/authorize?{urlencode(query)}"
        else:
            query = {
                "client_key": credentials["client_id"],
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "user.info.basic,video.upload,video.publish",
                "state": state,
            }
            auth_url = f"https://www.tiktok.com/v2/auth/authorize/?{urlencode(query)}"

        return {"status": "authorize", "platform": platform, "auth_url": auth_url}

    def complete_connection(self, platform: Platform, code: str, state: str) -> dict[str, Any]:
        pending = self._oauth_states.pop(state, None)
        if not pending or pending["platform"] != platform or pending["expires"] < time.time():
            raise SocialPublishError("This connection request expired. Return to Clip Farm Pilot and press Connect again.")

        credentials = self._credentials(platform)
        redirect_uri = pending["redirect_uri"]
        if platform == "youtube":
            payload = self._post_json(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": credentials["client_id"],
                    "client_secret": credentials["client_secret"],
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                },
            )
        elif platform == "instagram":
            payload = self._post_json(
                "https://api.instagram.com/oauth/access_token",
                data={
                    "client_id": credentials["client_id"],
                    "client_secret": credentials["client_secret"],
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                },
            )
        else:
            payload = self._post_json(
                "https://open.tiktokapis.com/v2/oauth/token/",
                data={
                    "client_key": credentials["client_id"],
                    "client_secret": credentials["client_secret"],
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri,
                },
            )

        token = payload.get("access_token")
        if not token:
            raise SocialPublishError(f"{PLATFORM_DETAILS[platform]['name']} did not return an access token.")

        account: dict[str, Any] = {
            "access_token": token,
            "refresh_token": payload.get("refresh_token"),
            "expires_at": time.time() + int(payload.get("expires_in") or 3600),
            "connected_at": time.time(),
        }
        if platform == "instagram":
            account["user_id"] = str(payload.get("user_id") or "")
        if platform == "tiktok":
            account["open_id"] = payload.get("open_id")

        try:
            account.update(self._profile(platform, account))
        except Exception:
            account["display_name"] = PLATFORM_DETAILS[platform]["name"]

        accounts = self._read_accounts()
        accounts[platform] = account
        self._write_accounts(accounts)
        return {"status": "connected", "platform": platform, "display_name": account["display_name"]}

    def disconnect(self, platform: Platform) -> dict[str, str]:
        if self._environment_account(platform):
            raise SocialPublishError("This account is supplied by the app environment and cannot be disconnected here.")
        accounts = self._read_accounts()
        accounts.pop(platform, None)
        self._write_accounts(accounts)
        return {"status": "disconnected", "platform": platform}

    def publish(
        self,
        export_id: str,
        platforms: list[Platform],
        title: str,
        caption: str,
        privacy: str,
    ) -> dict[str, Any]:
        source = self.exports_dir / f"{export_id}.mp4"
        if not source.is_file():
            raise SocialPublishError("The exported clip is no longer available. Export it again before publishing.")

        results = []
        for platform in platforms:
            account = self._account(platform)
            if not account:
                results.append({"platform": platform, "status": "error", "message": "Connect this account first."})
                continue
            try:
                if platform == "youtube":
                    result = self._publish_youtube(source, account, title, caption, privacy)
                elif platform == "instagram":
                    result = self._publish_instagram(source, account, caption or title)
                else:
                    result = self._publish_tiktok(source, account, caption or title, privacy)
                results.append({"platform": platform, "status": "published", **result})
            except Exception as exc:
                results.append({"platform": platform, "status": "error", "message": self._error_message(exc)})

        succeeded = sum(result["status"] == "published" for result in results)
        return {"results": results, "published": succeeded, "requested": len(platforms)}

    def _profile(self, platform: Platform, account: dict[str, Any]) -> dict[str, Any]:
        token = account["access_token"]
        if platform == "youtube":
            payload = self._get_json(
                "https://www.googleapis.com/youtube/v3/channels",
                params={"part": "snippet", "mine": "true"},
                headers={"Authorization": f"Bearer {token}"},
            )
            item = (payload.get("items") or [{}])[0]
            return {"display_name": item.get("snippet", {}).get("title") or "YouTube channel"}
        if platform == "instagram":
            payload = self._get_json(
                "https://graph.instagram.com/me",
                params={"fields": "id,username", "access_token": token},
            )
            return {
                "display_name": payload.get("username") or "Instagram account",
                "user_id": str(payload.get("id") or account.get("user_id") or ""),
            }
        payload = self._get_json(
            "https://open.tiktokapis.com/v2/user/info/",
            params={"fields": "open_id,display_name"},
            headers={"Authorization": f"Bearer {token}"},
        )
        user = payload.get("data", {}).get("user", {})
        return {
            "display_name": user.get("display_name") or "TikTok account",
            "open_id": user.get("open_id") or account.get("open_id"),
        }

    def _publish_youtube(
        self, source: Path, account: dict[str, Any], title: str, caption: str, privacy: str
    ) -> dict[str, Any]:
        token = self._fresh_token("youtube", account)
        privacy_status = privacy if privacy in {"public", "private", "unlisted"} else "private"
        metadata = {
            "snippet": {
                "title": (title or source.stem)[:100],
                "description": caption[:5000],
                "categoryId": "20",
            },
            "status": {"privacyStatus": privacy_status, "selfDeclaredMadeForKids": False},
        }
        start = self.session.post(
            "https://www.googleapis.com/upload/youtube/v3/videos",
            params={"uploadType": "resumable", "part": "snippet,status"},
            json=metadata,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Upload-Content-Length": str(source.stat().st_size),
                "X-Upload-Content-Type": "video/mp4",
            },
            timeout=30,
        )
        self._raise_for_api(start, "YouTube could not start the upload")
        upload_url = start.headers.get("Location")
        if not upload_url:
            raise SocialPublishError("YouTube did not return an upload location.")
        with source.open("rb") as video:
            uploaded = self.session.put(
                upload_url,
                data=video,
                headers={"Content-Type": "video/mp4", "Content-Length": str(source.stat().st_size)},
                timeout=900,
            )
        payload = self._response_json(uploaded, "YouTube upload failed")
        video_id = payload.get("id")
        return {
            "message": f"Uploaded to YouTube as {privacy_status}.",
            "post_id": video_id,
            "url": f"https://youtu.be/{video_id}" if video_id else "https://studio.youtube.com/",
        }

    def _publish_instagram(self, source: Path, account: dict[str, Any], caption: str) -> dict[str, Any]:
        token = self._fresh_token("instagram", account)
        user_id = str(account.get("user_id") or "")
        if not user_id:
            raise SocialPublishError("Reconnect Instagram so Clip Farm Pilot can identify the professional account.")
        version = str(env("META_API_VERSION", "v24.0"))
        created = self._post_json(
            f"https://graph.instagram.com/{version}/{user_id}/media",
            data={
                "media_type": "REELS",
                "upload_type": "resumable",
                "caption": caption[:2200],
                "share_to_feed": "true",
                "access_token": token,
            },
        )
        container_id = created.get("id")
        if not container_id:
            raise SocialPublishError("Instagram did not create a Reel upload container.")
        with source.open("rb") as video:
            upload = self.session.post(
                f"https://rupload.facebook.com/ig-api-upload/{version}/{container_id}",
                data=video,
                headers={
                    "Authorization": f"OAuth {token}",
                    "offset": "0",
                    "file_size": str(source.stat().st_size),
                    "Content-Type": "application/octet-stream",
                },
                timeout=900,
            )
        self._raise_for_api(upload, "Instagram video upload failed")

        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            status = self._get_json(
                f"https://graph.instagram.com/{version}/{container_id}",
                params={"fields": "status_code,status", "access_token": token},
            )
            code = status.get("status_code")
            if code == "FINISHED":
                break
            if code in {"ERROR", "EXPIRED"}:
                raise SocialPublishError(status.get("status") or "Instagram could not process the Reel.")
            time.sleep(3)
        else:
            raise SocialPublishError("Instagram is still processing the Reel. Try publishing again shortly.")

        published = self._post_json(
            f"https://graph.instagram.com/{version}/{user_id}/media_publish",
            data={"creation_id": container_id, "access_token": token},
        )
        return {
            "message": "Published to Instagram Reels.",
            "post_id": published.get("id"),
            "url": "https://www.instagram.com/",
        }

    def _publish_tiktok(
        self, source: Path, account: dict[str, Any], caption: str, privacy: str
    ) -> dict[str, Any]:
        token = self._fresh_token("tiktok", account)
        creator = self._post_json(
            "https://open.tiktokapis.com/v2/post/publish/creator_info/query/",
            headers={"Authorization": f"Bearer {token}"},
            json={},
        ).get("data", {})
        options = creator.get("privacy_level_options") or ["SELF_ONLY"]
        desired = "PUBLIC_TO_EVERYONE" if privacy == "public" else "SELF_ONLY"
        privacy_level = desired if desired in options else options[0]

        total_size = source.stat().st_size
        chunk_size = total_size if total_size < 5 * 1024 * 1024 else min(total_size, 32 * 1024 * 1024)
        chunk_count = max(1, math.ceil(total_size / chunk_size))
        initialized = self._post_json(
            "https://open.tiktokapis.com/v2/post/publish/video/init/",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "post_info": {
                    "title": caption[:2200],
                    "privacy_level": privacy_level,
                    "disable_duet": False,
                    "disable_comment": False,
                    "disable_stitch": False,
                    "video_cover_timestamp_ms": 1000,
                },
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": total_size,
                    "chunk_size": chunk_size,
                    "total_chunk_count": chunk_count,
                },
            },
        )
        data = initialized.get("data", {})
        upload_url = data.get("upload_url")
        publish_id = data.get("publish_id")
        if not upload_url:
            raise SocialPublishError("TikTok did not return an upload location.")

        with source.open("rb") as video:
            offset = 0
            while offset < total_size:
                chunk = video.read(chunk_size)
                end = offset + len(chunk) - 1
                response = self.session.put(
                    upload_url,
                    data=chunk,
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {offset}-{end}/{total_size}",
                    },
                    timeout=300,
                )
                self._raise_for_api(response, "TikTok video upload failed")
                offset = end + 1
        return {
            "message": f"Sent to TikTok with {privacy_level.lower().replace('_', ' ')} visibility.",
            "post_id": publish_id,
            "url": "https://www.tiktok.com/",
        }

    def _fresh_token(self, platform: Platform, account: dict[str, Any]) -> str:
        expires_at = float(account.get("expires_at") or 0)
        if not expires_at or expires_at > time.time() + 120:
            return str(account["access_token"])
        refresh_token = account.get("refresh_token")
        if not refresh_token or platform == "instagram":
            raise SocialPublishError(f"Your {PLATFORM_DETAILS[platform]['name']} connection expired. Disconnect and reconnect it.")
        credentials = self._credentials(platform)
        if platform == "youtube":
            payload = self._post_json(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": credentials["client_id"],
                    "client_secret": credentials["client_secret"],
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        else:
            payload = self._post_json(
                "https://open.tiktokapis.com/v2/oauth/token/",
                data={
                    "client_key": credentials["client_id"],
                    "client_secret": credentials["client_secret"],
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        account.update({
            "access_token": payload["access_token"],
            "refresh_token": payload.get("refresh_token") or refresh_token,
            "expires_at": time.time() + int(payload.get("expires_in") or 3600),
        })
        accounts = self._read_accounts()
        accounts[platform] = account
        self._write_accounts(accounts)
        return str(account["access_token"])

    def _get_json(self, url: str, **kwargs: Any) -> dict[str, Any]:
        response = self.session.get(url, timeout=30, **kwargs)
        return self._response_json(response, "The social platform request failed")

    def _post_json(self, url: str, **kwargs: Any) -> dict[str, Any]:
        response = self.session.post(url, timeout=30, **kwargs)
        return self._response_json(response, "The social platform request failed")

    def _response_json(self, response: Any, prefix: str) -> dict[str, Any]:
        self._raise_for_api(response, prefix)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SocialPublishError(f"{prefix}: an unreadable response was returned.") from exc
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict) and error.get("code") not in {None, "ok", 0}:
            raise SocialPublishError(error.get("message") or error.get("code") or prefix)
        return payload

    @staticmethod
    def _raise_for_api(response: Any, prefix: str) -> None:
        if 200 <= int(response.status_code) < 300:
            return
        try:
            payload = response.json()
            error = payload.get("error") or payload
            if isinstance(error, dict):
                message = error.get("message") or error.get("code")
            else:
                message = str(error)
        except Exception:
            message = getattr(response, "text", "")[:240]
        raise SocialPublishError(f"{prefix}: {message or f'HTTP {response.status_code}'}")

    @staticmethod
    def _error_message(exc: Exception) -> str:
        if isinstance(exc, SocialPublishError):
            return str(exc)
        if isinstance(exc, requests.RequestException):
            return "The upload could not reach the platform. Check your internet connection and try again."
        return str(exc) or "The platform could not publish this clip."
