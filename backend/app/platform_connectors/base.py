from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PublishResult:
    post_id: str
    status: str
    raw: dict


class PlatformPublisher(ABC):
    @abstractmethod
    def authorize(self, state: str) -> str: ...

    @abstractmethod
    def exchange_code(self, code: str, state: str | None = None) -> dict: ...

    @abstractmethod
    def validate_account(self, credentials: dict) -> dict: ...

    @abstractmethod
    def upload_media(self, media: Path, credentials: dict, metadata: dict) -> dict: ...

    @abstractmethod
    def publish(self, upload: dict, credentials: dict, metadata: dict) -> PublishResult: ...

    @abstractmethod
    def get_status(self, post_id: str, credentials: dict) -> str: ...

    def retry(self, operation, *, attempts: int = 5):
        import httpx
        import random
        import time

        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                return operation()
            except Exception as exc:
                last_error = exc
                if isinstance(exc, httpx.HTTPStatusError):
                    status = exc.response.status_code
                    if 400 <= status < 500 and status != 408:
                        raise
                if attempt + 1 == attempts:
                    break
                time.sleep(random.uniform(0, min(30, 2 ** attempt)))
        assert last_error is not None
        raise last_error

    def refresh_credentials(self, credentials: dict) -> dict:
        return credentials

    @abstractmethod
    def revoke(self, credentials: dict) -> None: ...
