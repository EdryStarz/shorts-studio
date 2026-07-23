from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.database.models import Clip, Publication, PublishingPreference, PublishStatus, Source
from app.database.session import SessionLocal
from app.scheduler.service import dispatch_due, recover_interrupted
import app.scheduler.service as scheduler_service


def test_sqlite_preference_cannot_bypass_disabled_real_publishing(monkeypatch, tmp_path):
    media = tmp_path / "blocked.mp4"
    media.write_bytes(b"video")
    with SessionLocal() as db:
        source = Source(
            original_name="source.mp4", storage_path=str(tmp_path / "source.mp4"),
            checksum="z" * 64, size_bytes=1, rights_confirmed=True,
        )
        db.add(source); db.flush()
        clip = Clip(
            source_id=source.id, start=0, end=20, score=90,
            export_path=str(media), checksum="y" * 64,
        )
        db.add(clip); db.flush()
        publication = Publication(
            clip_id=clip.id, platform="instagram",
            scheduled_for=datetime.now(timezone.utc) + timedelta(hours=1),
            dry_run=False, user_confirmed=True,
        )
        db.add_all([publication, PublishingPreference(id=1, enabled=True)])
        db.commit()
        monkeypatch.setattr(
            scheduler_service, "get_settings",
            lambda: SimpleNamespace(real_publishing_enabled=False),
        )
        monkeypatch.setattr(
            scheduler_service, "get_publisher",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("publisher must not be opened")
            ),
        )
        with pytest.raises(RuntimeError, match="disabled by configuration"):
            scheduler_service._publish(db, publication)


def test_due_dry_run_is_published_once(tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"valid-enough-for-mock")
    with SessionLocal() as db:
        source = Source(
            original_name="source.mp4", storage_path=str(tmp_path / "source.mp4"), checksum="a" * 64,
            size_bytes=1, rights_confirmed=True,
        )
        db.add(source)
        db.flush()
        clip = Clip(
            source_id=source.id, start=0, end=20, score=80, export_path=str(media),
            checksum="b" * 64, subtitle_text="A useful short",
        )
        db.add(clip)
        db.flush()
        publication = Publication(
            clip_id=clip.id, platform="youtube",
            scheduled_for=datetime.now(timezone.utc) - timedelta(minutes=1), dry_run=True,
        )
        db.add(publication)
        db.commit()
        publication_id = publication.id
    assert dispatch_due() == 1
    assert dispatch_due() == 0
    with SessionLocal() as db:
        saved = db.get(Publication, publication_id)
        assert saved.status == PublishStatus.dry_run
        assert saved.platform_post_id.startswith("mock-")
        assert saved.attempts == 1


def test_processing_publication_is_requeued_after_restart(tmp_path):
    with SessionLocal() as db:
        source = Source(
            original_name="source.mp4", storage_path=str(tmp_path / "source.mp4"), checksum="c" * 64,
            size_bytes=1, rights_confirmed=True,
        )
        db.add(source)
        db.flush()
        clip = Clip(source_id=source.id, start=0, end=20, score=80, checksum="d" * 64)
        db.add(clip)
        db.flush()
        publication = Publication(
            clip_id=clip.id,
            platform="tiktok",
            scheduled_for=datetime.now(timezone.utc) - timedelta(minutes=2),
            status=PublishStatus.processing,
            attempts=2,
        )
        db.add(publication)
        db.commit()
        publication_id = publication.id

    assert recover_interrupted() == 1
    with SessionLocal() as db:
        saved = db.get(Publication, publication_id)
        assert saved.status == PublishStatus.scheduled
        assert saved.attempts == 2
        assert "interrupted" in saved.error_log[-1]["error"]


def test_retry_with_platform_post_id_only_confirms_existing_post(monkeypatch, tmp_path):
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"already-uploaded")

    class ExistingPostPublisher:
        def retry(self, operation, **kwargs):
            return operation()

        def get_status(self, post_id, credentials):
            assert post_id == "existing-post"
            return "FINISHED"

        def upload_media(self, *args, **kwargs):
            raise AssertionError("an existing platform post must never be uploaded again")

        def publish(self, *args, **kwargs):
            raise AssertionError("an existing platform post must never be published again")

    monkeypatch.setattr(
        scheduler_service, "get_publisher", lambda platform, mock=False: ExistingPostPublisher(),
    )
    with SessionLocal() as db:
        source = Source(
            original_name="source.mp4", storage_path=str(tmp_path / "source.mp4"),
            checksum="e" * 64, size_bytes=1, rights_confirmed=True,
        )
        db.add(source)
        db.flush()
        clip = Clip(
            source_id=source.id, start=0, end=20, score=80,
            export_path=str(media), checksum="f" * 64,
        )
        db.add(clip)
        db.flush()
        publication = Publication(
            clip_id=clip.id, platform="instagram", dry_run=True,
            scheduled_for=datetime.now(timezone.utc) - timedelta(minutes=1),
            platform_post_id="existing-post",
        )
        db.add(publication)
        db.commit()
        publication_id = publication.id

    assert dispatch_due() == 1
    with SessionLocal() as db:
        saved = db.get(Publication, publication_id)
        assert saved.status == PublishStatus.dry_run
        assert saved.publish_metadata["platform_status"] == "FINISHED"
