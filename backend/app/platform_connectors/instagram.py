from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode

import httpx

from app.core.config import get_settings
from app.platform_connectors.base import PlatformPublisher, PublishResult
from app.platform_connectors.oauth_config import get_oauth_config


class InstagramPublisher(PlatformPublisher):
    graph = "https://graph.instagram.com"

    @staticmethod
    def _json(response: httpx.Response, fallback: str) -> dict:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.is_error:
            error = payload.get("error", {})
            message = error.get("message", fallback)
            code = error.get("code", response.status_code)
            raise httpx.HTTPStatusError(
                f"Instagram {code}: {message}", request=response.request, response=response,
            )
        return payload

    def authorize(self, state: str) -> str:
        config = get_oauth_config("instagram")
        if not config.configured:
            raise ValueError("Instagram OAuth application credentials are not configured")
        return "https://www.instagram.com/oauth/authorize?" + urlencode({
            "client_id": config.client_id, "redirect_uri": config.redirect_uri,
            "response_type": "code", "scope": "instagram_business_basic,instagram_business_content_publish",
            "state": state,
        })

    def exchange_code(self, code: str, state: str | None = None) -> dict:
        config = get_oauth_config("instagram")
        response = httpx.post("https://api.instagram.com/oauth/access_token", data={
            "client_id": config.client_id, "client_secret": config.client_secret,
            "grant_type": "authorization_code", "redirect_uri": config.redirect_uri, "code": code,
        }, timeout=30)
        short_lived = self._json(response, "Instagram authorization code exchange failed")
        exchange = httpx.get(f"{self.graph}/access_token", params={
            "grant_type": "ig_exchange_token",
            "client_secret": config.client_secret,
            "access_token": short_lived["access_token"],
        }, timeout=30)
        long_lived = self._json(exchange, "Instagram long-lived token exchange failed")
        credentials = {**short_lived, **long_lived}
        # Instagram's current Login API may omit ``expires_in`` even after a
        # successful long-lived token exchange. Treat the documented 60-day
        # lifetime as the default so the scheduler does not immediately try to
        # refresh a brand-new token (which Instagram rejects).
        credentials["expires_in"] = float(credentials.get("expires_in") or 5184000)
        credentials["expires_at"] = time.time() + credentials["expires_in"]
        return credentials

    def validate_account(self, credentials: dict) -> dict:
        response = httpx.get(f"{self.graph}/me", params={
            "fields": "id,username,account_type", "access_token": credentials["access_token"],
        }, timeout=30)
        data = self._json(response, "Instagram account validation failed")
        return {"valid": data.get("account_type") in {"BUSINESS", "MEDIA_CREATOR"}, "id": data["id"], "name": data.get("username", "")}

    def refresh_credentials(self, credentials: dict) -> dict:
        if float(credentials.get("expires_at", 0)) > time.time() + 86400:
            return credentials
        if not credentials.get("access_token"):
            return credentials
        response = httpx.get(f"{self.graph}/refresh_access_token", params={
            "grant_type": "ig_refresh_token", "access_token": credentials["access_token"],
        }, timeout=30)
        updated = {**credentials, **self._json(response, "Instagram token refresh failed")}
        updated["expires_at"] = time.time() + float(updated.get("expires_in", 5184000))
        return updated

    def upload_media(self, media: Path, credentials: dict, metadata: dict) -> dict:
        account_id = credentials.get("user_id") or self.validate_account(credentials)["id"]
        response = httpx.post(f"{self.graph}/{account_id}/media", data={
            "media_type": "REELS", "upload_type": "resumable", "caption": metadata["title"],
            "share_to_feed": "true", "access_token": credentials["access_token"],
        }, timeout=30)
        try:
            container = self._json(response, "could not create a resumable Reel container")
        except httpx.HTTPStatusError as exc:
            # Some eligible Instagram accounts still reject the documented
            # resumable flow with "video_url is required". Expose only this
            # exact file via an unguessable, short-lived HTTPS URL for Meta.
            if "video_url is required" not in str(exc).lower():
                raise
            return self._upload_via_temporary_https(media, credentials, metadata, account_id)
        container_id = container["id"]
        upload_uri = container.get("uri")
        if not upload_uri or not upload_uri.startswith("https://rupload.facebook.com/"):
            raise RuntimeError("Instagram did not return a trusted resumable upload URI")

        size = media.stat().st_size
        with media.open("rb") as stream:
            uploaded = httpx.post(
                upload_uri,
                headers={
                    "Authorization": f"OAuth {credentials['access_token']}",
                    "offset": "0",
                    "file_size": str(size),
                    "Content-Length": str(size),
                    "Content-Type": "application/octet-stream",
                },
                content=stream,
                timeout=httpx.Timeout(300.0, connect=30.0, pool=30.0),
            )
        result = self._json(uploaded, "local Reel upload failed")
        if result.get("success") is False:
            raise RuntimeError(result.get("message", "Instagram rejected the local Reel upload"))

        self._wait_until_ready(container_id, credentials["access_token"])
        return {"container_id": container_id, "account_id": account_id}

    @staticmethod
    def _cloudflared_path() -> Path:
        candidates = [get_settings().storage_root / "tools" / "cloudflared.exe"]
        if getattr(sys, "frozen", False):
            candidates.insert(0, Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "bin" / "cloudflared.exe")
        located = shutil.which("cloudflared")
        if located:
            candidates.append(Path(located))
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        raise RuntimeError("Instagram HTTPS publishing helper cloudflared.exe was not found")

    @staticmethod
    def _media_handler(media: Path, secret: str):
        route = f"/{secret}.mp4"

        class MediaHandler(BaseHTTPRequestHandler):
            server_version = "ShortsStudioMedia/1"

            def do_HEAD(self) -> None:  # noqa: N802
                self._serve(False)

            def do_GET(self) -> None:  # noqa: N802
                self._serve(True)

            def _serve(self, body: bool) -> None:
                if self.path.split("?", 1)[0] != route:
                    self.send_error(404)
                    return
                size = media.stat().st_size
                start, end = 0, size - 1
                header = self.headers.get("Range", "")
                if header.startswith("bytes="):
                    try:
                        first, last = header[6:].split("-", 1)
                        start = int(first) if first else 0
                        end = min(int(last), size - 1) if last else size - 1
                        if start < 0 or start > end:
                            raise ValueError
                    except ValueError:
                        self.send_error(416)
                        return
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                else:
                    self.send_response(200)
                length = end - start + 1
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "private, max-age=300")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                if not body:
                    return
                with media.open("rb") as stream:
                    stream.seek(start)
                    remaining = length
                    while remaining:
                        chunk = stream.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)

            def log_message(self, fmt: str, *args: object) -> None:
                return

        return MediaHandler

    @staticmethod
    def _wait_for_tunnel(process: subprocess.Popen[str]) -> str:
        lines: queue.Queue[str | None] = queue.Queue()

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                lines.put(line)
            lines.put(None)

        threading.Thread(target=read_output, daemon=True).start()
        pattern = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
        deadline = time.monotonic() + 60
        public_url = ""
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=min(1, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                if process.poll() is not None:
                    break
                continue
            if line is None:
                break
            match = pattern.search(line)
            if match:
                public_url = match.group(0)
            if public_url and "Registered tunnel connection" in line:
                time.sleep(10)
                return public_url
        raise RuntimeError("temporary Instagram HTTPS tunnel did not become ready")

    def _upload_via_temporary_https(
        self, media: Path, credentials: dict, metadata: dict, account_id: str,
    ) -> dict:
        secret = secrets.token_urlsafe(32)
        server = ThreadingHTTPServer(("127.0.0.1", 0), self._media_handler(media, secret))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(
            [str(self._cloudflared_path()), "tunnel", "--url", f"http://127.0.0.1:{server.server_port}", "--no-autoupdate"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", creationflags=flags,
        )
        try:
            public_url = self._wait_for_tunnel(process)
            response = httpx.post(f"{self.graph}/{account_id}/media", data={
                "media_type": "REELS", "video_url": f"{public_url}/{secret}.mp4",
                "caption": metadata["title"], "share_to_feed": "true",
                "access_token": credentials["access_token"],
            }, timeout=45)
            container = self._json(response, "could not create a Reel container from the temporary HTTPS URL")
            container_id = container["id"]
            self._wait_until_ready(container_id, credentials["access_token"])
            return {"container_id": container_id, "account_id": account_id}
        finally:
            server.shutdown()
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()

    def _wait_until_ready(self, container_id: str, access_token: str) -> None:
        for attempt in range(36):
            response = httpx.get(f"{self.graph}/{container_id}", params={
                "fields": "status_code,status", "access_token": access_token,
            }, timeout=30)
            data = self._json(response, "could not read Reel processing status")
            status = data.get("status_code", "")
            if status == "FINISHED":
                return
            if status in {"ERROR", "EXPIRED"}:
                raise RuntimeError(data.get("status", f"Instagram Reel processing {status.lower()}"))
            if attempt + 1 < 36:
                time.sleep(5)
        raise TimeoutError("Instagram Reel processing did not finish within 3 minutes")

    def publish(self, upload: dict, credentials: dict, metadata: dict) -> PublishResult:
        response = httpx.post(f"{self.graph}/{upload['account_id']}/media_publish", data={
            "creation_id": upload["container_id"], "access_token": credentials["access_token"],
        }, timeout=30)
        data = self._json(response, "Instagram Reel publishing failed")
        post_id = data["id"]
        return PublishResult(post_id, "published", data)

    def get_status(self, post_id: str, credentials: dict) -> str:
        response = httpx.get(f"{self.graph}/{post_id}", params={
            # ``status_code`` belongs to an unpublished creation container and
            # is not a field on the media object returned by ``media_publish``.
            # Once that object can be read back by id, Meta has accepted the
            # publication and it is safe to mark the queue row complete.
            "fields": "id", "access_token": credentials["access_token"],
        }, timeout=30)
        data = self._json(response, "Instagram post status lookup failed")
        if str(data.get("id")) != str(post_id):
            raise RuntimeError("Instagram returned a different media id while confirming the Reel")
        return "FINISHED"

    def revoke(self, credentials: dict) -> None:
        httpx.delete(f"{self.graph}/me/permissions", params={"access_token": credentials["access_token"]}, timeout=30).raise_for_status()
