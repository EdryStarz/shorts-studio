from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app.core.config import get_settings
from app.core.secrets import decrypt_credentials, encrypt_credentials
from app.database.models import (
    Clip, ConnectedAccount, Publication, PublishingPreference, PublishStatus,
)
from app.database.session import SessionLocal
from app.platform_connectors.registry import get_publisher
from app.publishing_metadata import build_publication_metadata


def recover_interrupted() -> int:
    """Return publications left processing by a previous app process to the queue."""
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        interrupted = db.scalars(
            select(Publication).where(Publication.status == PublishStatus.processing)
        ).all()
        for publication in interrupted:
            publication.status = PublishStatus.scheduled
            publication.next_attempt_at = now
            publication.error_log = [*publication.error_log, {
                "at": now.isoformat(),
                "attempt": publication.attempts,
                "error": "publication was interrupted by an application restart and re-queued",
            }]
        db.commit()
        return len(interrupted)


def dispatch_due() -> int:
    now = datetime.now(timezone.utc)
    processed = 0
    with SessionLocal() as db:
        due = db.scalars(
            select(Publication).where(
                Publication.status == PublishStatus.scheduled,
                Publication.scheduled_for <= now,
                (
                    Publication.next_attempt_at.is_(None)
                    | (Publication.next_attempt_at <= now)
                ),
            ).order_by(Publication.scheduled_for).limit(20)
        ).all()
        for publication in due:
            publication.status = PublishStatus.processing
            publication.attempts += 1
            db.commit()
            try:
                _publish(db, publication)
                processed += 1
            except Exception as exc:
                publication.error_log = [*publication.error_log, {
                    "at": now.isoformat(), "attempt": publication.attempts, "error": str(exc)[:1000],
                }]
                if publication.attempts < 5:
                    publication.status = PublishStatus.scheduled
                    publication.next_attempt_at = now + timedelta(minutes=2 ** publication.attempts)
                else:
                    publication.status = PublishStatus.failed
                    publication.next_attempt_at = None
                db.commit()
    return processed


def _publish(db, publication: Publication) -> None:
    settings = get_settings()
    clip = db.get(Clip, publication.clip_id)
    if not clip or not clip.export_path or not Path(clip.export_path).is_file():
        raise RuntimeError("rendered clip is unavailable")
    if publication.dry_run:
        publisher = get_publisher(publication.platform, mock=True)
        credentials = {"access_token": "dry-run"}
    else:
        preference = db.get(PublishingPreference, 1)
        if not settings.real_publishing_enabled:
            raise RuntimeError("real publishing is disabled by configuration")
        if not preference or not preference.enabled:
            raise RuntimeError("real publishing is disabled in application settings")
        if not publication.user_confirmed:
            raise RuntimeError("explicit user confirmation is required")
        account = db.get(ConnectedAccount, publication.account_id)
        if not account or account.revoked:
            raise RuntimeError("a valid connected account is required")
        publisher = get_publisher(publication.platform)
        credentials = decrypt_credentials(account.encrypted_credentials)
        refreshed = publisher.refresh_credentials(credentials)
        if refreshed != credentials:
            account.encrypted_credentials = encrypt_credentials(refreshed)
            db.commit()
        credentials = refreshed
    metadata = publication.publish_metadata or build_publication_metadata(
        f"{clip.title}. {clip.subtitle_text or clip.transcript}", publication.platform,
    )
    accepted = {
        "uploaded", "processed", "published", "publish_complete", "finished",
    }
    # A platform id proves that the create/publish call already succeeded.
    # Retries and restart recovery must only confirm that existing post; doing
    # another upload here can create duplicate public Reels/Shorts.
    if publication.platform_post_id:
        platform_status = publisher.retry(
            lambda: publisher.get_status(publication.platform_post_id, credentials)
        )
        if str(platform_status).casefold() not in accepted:
            raise RuntimeError(
                f"platform did not confirm publication yet ({platform_status})"
            )
        publication.publish_metadata = {
            **metadata,
            "platform_status": platform_status,
            "platform_confirmed_at": datetime.now(timezone.utc).isoformat(),
        }
        publication.next_attempt_at = None
        publication.status = PublishStatus.dry_run if publication.dry_run else PublishStatus.published
        db.commit()
        return
    upload = publisher.retry(lambda: publisher.upload_media(Path(clip.export_path), credentials, metadata))
    result = publisher.retry(lambda: publisher.publish(upload, credentials, metadata))
    publication.platform_post_id = result.post_id
    # Persist the id before the status lookup. If the process is interrupted or
    # confirmation fails, the next attempt queries this post instead of creating another.
    db.commit()
    platform_status = result.status
    if not publication.dry_run:
        platform_status = publisher.retry(
            lambda: publisher.get_status(result.post_id, credentials)
        )
        if str(platform_status).casefold() not in accepted:
            raise RuntimeError(
                f"platform did not confirm publication yet ({platform_status})"
            )
    publication.publish_metadata = {
        **metadata,
        "platform_status": platform_status,
        "platform_confirmed_at": datetime.now(timezone.utc).isoformat(),
    }
    publication.next_attempt_at = None
    publication.status = PublishStatus.dry_run if publication.dry_run else PublishStatus.published
    db.commit()
