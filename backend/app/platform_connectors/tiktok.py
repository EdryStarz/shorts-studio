from pathlib import Path
import hashlib
import hmac
import math
import time
from urllib.parse import urlencode

import httpx

from app.platform_connectors.base import PlatformPublisher, PublishResult
from app.platform_connectors.oauth_config import get_oauth_config


class TikTokPublisher(PlatformPublisher):
    api = "https://open.tiktokapis.com"

    @staticmethod
    def _code_verifier(state: str, client_secret: str) -> str:
        """Derive a per-request PKCE verifier without persisting plaintext state."""
        return hmac.new(client_secret.encode(), state.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _select_privacy(requested: str, options: list[str]) -> str:
        if requested in options:
            return requested
        raise ValueError("requested TikTok privacy is not allowed for this creator")

    @staticmethod
    def _raise_api_error(response: httpx.Response, fallback: str) -> dict:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        error = payload.get("error", {})
        code = error.get("code", "")
        message = error.get("message", "") or fallback
        detail = f"TikTok {code or 'HTTP error'}: {message} (HTTP {response.status_code})"
        if response.is_error:
            raise httpx.HTTPStatusError(detail, request=response.request, response=response)
        if code and code != "ok":
            raise RuntimeError(detail)
        return payload

    def authorize(self, state: str) -> str:
        config = get_oauth_config("tiktok")
        if not config.configured:
            raise ValueError("TikTok OAuth application credentials are not configured")
        verifier = self._code_verifier(state, config.client_secret)
        challenge = hashlib.sha256(verifier.encode()).hexdigest()
        return "https://www.tiktok.com/v2/auth/authorize/?" + urlencode({
            "client_key": config.client_id, "scope": "user.info.basic,video.publish",
            "response_type": "code", "redirect_uri": config.redirect_uri, "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256",
        })

    def exchange_code(self, code: str, state: str | None = None) -> dict:
        config = get_oauth_config("tiktok")
        if not state:
            raise ValueError("TikTok OAuth state is required for PKCE token exchange")
        response = httpx.post(f"{self.api}/v2/oauth/token/", data={
            "client_key": config.client_id, "client_secret": config.client_secret,
            "code": code, "grant_type": "authorization_code", "redirect_uri": config.redirect_uri,
            "code_verifier": self._code_verifier(state, config.client_secret),
        }, timeout=30)
        response.raise_for_status()
        return response.json()

    def validate_account(self, credentials: dict) -> dict:
        response = httpx.post(
            f"{self.api}/v2/post/publish/creator_info/query/",
            headers={"Authorization": f"Bearer {credentials['access_token']}", "Content-Type": "application/json; charset=UTF-8"},
            json={}, timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error", {}).get("code") != "ok":
            raise RuntimeError(payload.get("error", {}).get("message", "TikTok creator validation failed"))
        data = payload["data"]
        return {"valid": True, "id": data.get("creator_username", ""), "name": data.get("creator_nickname", ""), **data}

    def refresh_credentials(self, credentials: dict) -> dict:
        if float(credentials.get("expires_at", 0)) > time.time() + 60:
            return credentials
        refresh_token = credentials.get("refresh_token")
        if not refresh_token:
            return credentials
        config = get_oauth_config("tiktok")
        response = httpx.post(f"{self.api}/v2/oauth/token/", data={
            "client_key": config.client_id,
            "client_secret": config.client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }, timeout=30)
        response.raise_for_status()
        updated = {**credentials, **response.json()}
        updated["expires_at"] = time.time() + float(updated.get("expires_in", 86400))
        return updated

    def upload_media(self, media: Path, credentials: dict, metadata: dict) -> dict:
        creator = self.validate_account(credentials)
        requested_privacy = metadata.get("privacy")
        if not requested_privacy:
            raise ValueError("TikTok privacy must be selected explicitly by the creator")
        privacy = self._select_privacy(requested_privacy, creator.get("privacy_level_options", []))
        size = media.stat().st_size
        max_chunk = 64 * 1024 * 1024
        total_chunk_count = max(1, math.ceil(size / max_chunk))
        chunk_size = size if total_chunk_count == 1 else size // total_chunk_count
        headers = {
            "Authorization": f"Bearer {credentials['access_token']}",
            "Content-Type": "application/json; charset=UTF-8",
        }

        def initialize(selected_privacy: str):
            return httpx.post(
                f"{self.api}/v2/post/publish/video/init/",
                headers=headers,
                json={
                    "post_info": {
                        "title": metadata["title"], "privacy_level": selected_privacy,
                        "disable_duet": not bool(metadata.get("allow_duet", False)),
                        "disable_comment": not bool(metadata.get("allow_comment", False)),
                        "disable_stitch": not bool(metadata.get("allow_stitch", False)),
                        "brand_content_toggle": bool(metadata.get("brand_content_toggle", False)),
                        "brand_organic_toggle": bool(metadata.get("brand_organic_toggle", False)),
                    },
                    "source_info": {
                        "source": "FILE_UPLOAD", "video_size": size,
                        "chunk_size": chunk_size, "total_chunk_count": total_chunk_count,
                    },
                }, timeout=30,
            )

        response = initialize(privacy)
        data = self._raise_api_error(response, "TikTok upload initialization failed")
        with media.open("rb") as stream:
            offset = 0
            for index in range(total_chunk_count):
                length = size - offset if index + 1 == total_chunk_count else chunk_size
                content = stream.read(length)
                upload = httpx.put(data["data"]["upload_url"], content=content, headers={
                    "Content-Type": "video/mp4", "Content-Length": str(length),
                    "Content-Range": f"bytes {offset}-{offset + length - 1}/{size}",
                }, timeout=httpx.Timeout(180.0, connect=15.0, pool=15.0))
                upload.raise_for_status()
                offset += length
        return {"publish_id": data["data"]["publish_id"]}

    def publish(self, upload: dict, credentials: dict, metadata: dict) -> PublishResult:
        post_id = upload["publish_id"]
        status = "PROCESSING_UPLOAD"
        for attempt in range(48):
            status = self.get_status(post_id, credentials)
            if status == "PUBLISH_COMPLETE":
                return PublishResult(post_id, status, upload)
            if status == "FAILED":
                raise RuntimeError("TikTok rejected the post while processing it")
            if attempt + 1 < 48:
                time.sleep(5)
        raise TimeoutError(f"TikTok post is still processing after 4 minutes ({status})")

    def get_status(self, post_id: str, credentials: dict) -> str:
        response = httpx.post(
            f"{self.api}/v2/post/publish/status/fetch/", json={"publish_id": post_id},
            headers={"Authorization": f"Bearer {credentials['access_token']}", "Content-Type": "application/json; charset=UTF-8"}, timeout=30,
        )
        payload = self._raise_api_error(response, "TikTok publish status is unavailable")
        return payload.get("data", {}).get("status", "unknown")

    def revoke(self, credentials: dict) -> None:
        config = get_oauth_config("tiktok")
        httpx.post(f"{self.api}/v2/oauth/revoke/", data={
            "client_key": config.client_id, "client_secret": config.client_secret,
            "token": credentials["access_token"],
        }, timeout=30).raise_for_status()
