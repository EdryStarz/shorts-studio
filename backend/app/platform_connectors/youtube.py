from pathlib import Path
import time
from urllib.parse import urlencode

import httpx

from app.platform_connectors.base import PlatformPublisher, PublishResult
from app.platform_connectors.oauth_config import get_oauth_config


class YouTubePublisher(PlatformPublisher):
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url = "https://oauth2.googleapis.com/token"
    upload_url = "https://www.googleapis.com/upload/youtube/v3/videos"

    def authorize(self, state: str) -> str:
        config = get_oauth_config("youtube")
        if not config.configured:
            raise ValueError("YouTube OAuth application credentials are not configured")
        scope = " ".join((
            "https://www.googleapis.com/auth/youtube.force-ssl",
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.readonly",
        ))
        return f"{self.auth_url}?{urlencode({'client_id': config.client_id, 'redirect_uri': config.redirect_uri, 'response_type': 'code', 'scope': scope, 'access_type': 'offline', 'prompt': 'consent', 'include_granted_scopes': 'true', 'state': state})}"

    def exchange_code(self, code: str, state: str | None = None) -> dict:
        config = get_oauth_config("youtube")
        data = {
            "client_id": config.client_id, "code": code,
            "grant_type": "authorization_code", "redirect_uri": config.redirect_uri,
        }
        if config.client_secret:
            data["client_secret"] = config.client_secret
        response = httpx.post(self.token_url, data=data, timeout=30)
        response.raise_for_status()
        return response.json()

    def validate_account(self, credentials: dict) -> dict:
        response = httpx.get(
            "https://www.googleapis.com/youtube/v3/channels",
            params={"part": "snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {credentials['access_token']}"}, timeout=30,
        )
        response.raise_for_status()
        items = response.json().get("items", [])
        return {"valid": bool(items), "id": items[0]["id"] if items else "", "name": items[0]["snippet"]["title"] if items else ""}

    def refresh_credentials(self, credentials: dict) -> dict:
        if float(credentials.get("expires_at", 0)) > time.time() + 60:
            return credentials
        refresh_token = credentials.get("refresh_token")
        if not refresh_token:
            return credentials
        config = get_oauth_config("youtube")
        data = {
            "client_id": config.client_id,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        if config.client_secret:
            data["client_secret"] = config.client_secret
        response = httpx.post(self.token_url, data=data, timeout=30)
        response.raise_for_status()
        updated = {**credentials, **response.json(), "refresh_token": refresh_token}
        updated["expires_at"] = time.time() + float(updated.get("expires_in", 3600))
        return updated

    def upload_media(self, media: Path, credentials: dict, metadata: dict) -> dict:
        size = media.stat().st_size
        status = {"privacyStatus": metadata.get("privacy", "private")}
        if metadata.get("publish_at"):
            status["privacyStatus"] = "private"
            status["publishAt"] = metadata["publish_at"]
        body = {
            "snippet": {
                "title": metadata["title"],
                "description": metadata.get("description", ""),
                "tags": metadata.get("tags", []),
            },
            "status": status,
        }
        response = httpx.post(
            self.upload_url,
            params={"uploadType": "resumable", "part": "snippet,status"}, json=body,
            headers={
                "Authorization": f"Bearer {credentials['access_token']}",
                "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4",
            }, timeout=30,
        )
        response.raise_for_status()
        location = response.headers.get("Location")
        if not location:
            raise RuntimeError("YouTube did not return a resumable upload URL")
        with media.open("rb") as stream:
            upload = httpx.put(location, content=stream, headers={
                "Authorization": f"Bearer {credentials['access_token']}",
                "Content-Type": "video/mp4", "Content-Length": str(size),
            }, timeout=1800)
        upload.raise_for_status()
        return upload.json()

    def publish(self, upload: dict, credentials: dict, metadata: dict) -> PublishResult:
        return PublishResult(upload["id"], upload.get("status", {}).get("uploadStatus", "uploaded"), upload)

    def get_status(self, post_id: str, credentials: dict) -> str:
        response = httpx.get(
            "https://www.googleapis.com/youtube/v3/videos", params={"part": "status", "id": post_id},
            headers={"Authorization": f"Bearer {credentials['access_token']}"}, timeout=30,
        )
        response.raise_for_status()
        items = response.json().get("items", [])
        return items[0]["status"]["uploadStatus"] if items else "not_found"

    def revoke(self, credentials: dict) -> None:
        token = credentials.get("refresh_token") or credentials.get("access_token")
        if token:
            httpx.post("https://oauth2.googleapis.com/revoke", data={"token": token}, timeout=30).raise_for_status()
