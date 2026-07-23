import hashlib
from pathlib import Path
from urllib.parse import urlencode

from app.platform_connectors.base import PlatformPublisher, PublishResult


class MockPublisher(PlatformPublisher):
    def __init__(self, platform: str = "mock") -> None:
        self.platform = platform

    def authorize(self, state: str) -> str:
        return f"http://localhost/mock-authorize?{urlencode({'state': state})}"

    def exchange_code(self, code: str, state: str | None = None) -> dict:
        return {"access_token": f"mock-{code}", "external_id": "mock-user"}

    def validate_account(self, credentials: dict) -> dict:
        return {"valid": bool(credentials.get("access_token")), "id": "mock-user", "name": "Mock account"}

    def upload_media(self, media: Path, credentials: dict, metadata: dict) -> dict:
        if not media.is_file():
            raise FileNotFoundError(media)
        return {"upload_id": hashlib.sha256(media.read_bytes()).hexdigest()[:16]}

    def publish(self, upload: dict, credentials: dict, metadata: dict) -> PublishResult:
        post_id = f"mock-{upload['upload_id']}"
        return PublishResult(post_id, "published", {"id": post_id})

    def get_status(self, post_id: str, credentials: dict) -> str:
        return "published"

    def revoke(self, credentials: dict) -> None:
        return None
