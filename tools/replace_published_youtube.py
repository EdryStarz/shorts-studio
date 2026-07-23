from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys

import httpx
from sqlalchemy import select


ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ["LOCALAPPDATA"]) / "ShortsStudio"
os.environ.update({
    "STORAGE_ROOT": str(DATA),
    "DATABASE_URL": f"sqlite:///{(DATA / 'shorts.db').as_posix()}",
    "TOKEN_ENCRYPTION_KEY": (DATA / "token.key").read_text(encoding="ascii").strip(),
})
sys.path.insert(0, str(ROOT / "backend"))

from app.core.secrets import decrypt_credentials, encrypt_credentials  # noqa: E402
from app.database.models import (  # noqa: E402
    ConnectedAccount, Publication, PublishStatus,
)
from app.database.session import SessionLocal  # noqa: E402
from app.platform_connectors.youtube import YouTubePublisher  # noqa: E402


REPLACEMENTS = {
    "1498db0e-a8b2-4e1e-a09f-4f6067a267fa": "9faQhRGBEIY",
    "f1397b60-2822-4418-b574-3ad4b09c9654": "RLhsBos0NPw",
}


def delete_video(access_token: str, video_id: str) -> None:
    response = httpx.delete(
        "https://www.googleapis.com/youtube/v3/videos",
        params={"id": video_id},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if response.status_code != 204:
        raise RuntimeError(
            f"YouTube delete failed for {video_id} ({response.status_code}): "
            f"{response.text}"
        )


def main() -> None:
    with SessionLocal() as db:
        account = db.scalar(select(ConnectedAccount).where(
            ConnectedAccount.platform == "youtube",
            ConnectedAccount.revoked.is_(False),
        ))
        if not account:
            raise RuntimeError("Connected YouTube account was not found")
        publisher = YouTubePublisher()
        credentials = decrypt_credentials(account.encrypted_credentials)
        refreshed = publisher.refresh_credentials(credentials)
        if refreshed != credentials:
            account.encrypted_credentials = encrypt_credentials(refreshed)
            db.commit()

        publications = {
            item.clip_id: item for item in db.scalars(select(Publication).where(
                Publication.clip_id.in_(REPLACEMENTS),
                Publication.platform == "youtube",
            )).all()
        }
        for clip_id, video_id in REPLACEMENTS.items():
            publication = publications.get(clip_id)
            if not publication or publication.platform_post_id != video_id:
                raise RuntimeError(
                    f"Refusing to delete unexpected publication for {clip_id}"
                )

        # Do not alter the local queue unless both exact remote videos are gone.
        for video_id in REPLACEMENTS.values():
            delete_video(refreshed["access_token"], video_id)
            print(f"deleted {video_id}")

        now = datetime.now(timezone.utc)
        for offset, clip_id in zip((5, 25), REPLACEMENTS, strict=True):
            publication = publications[clip_id]
            publication.platform_post_id = None
            publication.status = PublishStatus.scheduled
            publication.scheduled_for = now + timedelta(minutes=offset)
            publication.attempts = 0
            publication.error_log = []
        db.commit()
        for clip_id, publication in publications.items():
            print(f"scheduled {clip_id} for {publication.scheduled_for.isoformat()}")


if __name__ == "__main__":
    main()
