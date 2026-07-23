from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlalchemy import select

import app.scheduler.service as scheduler_service
from app.database.models import Clip, Publication, PublishStatus, Source
from app.database.session import SessionLocal
from app.main import app


def _valid_slot(days: int = 3, hour: int = 10) -> datetime:
    local = (datetime.now(ZoneInfo("Asia/Tbilisi")) + timedelta(days=days)).replace(
        hour=hour, minute=0, second=0, microsecond=0,
    )
    return local.astimezone(timezone.utc)


def test_publication_is_confirmed_by_get_persists_patch_and_rejects_duplicate(tmp_path: Path):
    media = tmp_path / "ready.mp4"
    media.write_bytes(b"ready-video")
    with SessionLocal() as db:
        source = Source(
            original_name="technical.mp4", title="Проверка очереди", author="Example Creator",
            storage_path=str(tmp_path / "source.mp4"), checksum="a" * 64,
            size_bytes=10, rights_confirmed=True,
        )
        db.add(source)
        db.flush()
        clip = Clip(
            source_id=source.id, sequence_number=1,
            title="Роботы устроили драку — реакция Хесуса",
            description="Хесус смотрит на бой роботов и реагирует на развязку.",
            hashtags=["jesusavgn", "хесус", "роботы", "реакция", "shorts"],
            processing_status="completed", start=0, end=25, score=90,
            transcript="Роботы устроили драку на ринге.", export_path=str(media),
        )
        db.add(clip)
        db.commit()
        clip_id = clip.id

    first_slot = _valid_slot()
    with TestClient(app) as client:
        created = client.post("/api/publications", json={
            "clip_id": clip_id, "platform": "youtube",
            "scheduled_for": first_slot.isoformat(), "timezone_name": "Asia/Tbilisi",
            "dry_run": True,
        })
        assert created.status_code == 201, created.text
        row = created.json()
        assert row["clip_id"] == clip_id
        assert row["timezone_name"] == "Asia/Tbilisi"
        assert row["clip_title"].startswith("Роботы")
        assert len(row["clip_hashtags"]) == 5

        confirmed = client.get(f"/api/publications/{row['id']}")
        assert confirmed.status_code == 200
        assert confirmed.json()["id"] == row["id"]
        duplicate = client.post("/api/publications", json={
            "clip_id": clip_id, "platform": "youtube",
            "scheduled_for": (_valid_slot(days=4)).isoformat(),
            "timezone_name": "Asia/Tbilisi", "dry_run": True,
        })
        assert duplicate.status_code == 409

        moved = first_slot + timedelta(hours=3)
        patched = client.patch(f"/api/publications/{row['id']}/schedule", json={
            "scheduled_for": moved.isoformat(), "timezone_name": "Asia/Tbilisi",
        })
        assert patched.status_code == 200, patched.text
        reloaded = client.get("/api/publications").json()
        saved = next(item for item in reloaded if item["id"] == row["id"])
        assert saved["scheduled_for"] == patched.json()["scheduled_for"]
        assert saved["requested_for"] == patched.json()["scheduled_for"]

    with SessionLocal() as db:
        rows = list(db.scalars(select(Publication).where(
            Publication.clip_id == clip_id, Publication.platform == "youtube",
        )).all())
        assert len(rows) == 1
        assert rows[0].timezone_name == "Asia/Tbilisi"


def test_retry_delay_does_not_rewrite_user_selected_time(monkeypatch, tmp_path: Path):
    media = tmp_path / "ready.mp4"
    media.write_bytes(b"ready-video")
    original = datetime.now(timezone.utc) - timedelta(minutes=1)
    with SessionLocal() as db:
        source = Source(
            original_name="source.mp4", title="Источник", storage_path=str(tmp_path / "source.mp4"),
            checksum="b" * 64, size_bytes=1, rights_confirmed=True,
        )
        db.add(source)
        db.flush()
        clip = Clip(
            source_id=source.id, start=0, end=25, score=90,
            title="Клип", description="Описание", hashtags=["a", "b", "c", "d", "e"],
            export_path=str(media), processing_status="completed",
        )
        db.add(clip)
        db.flush()
        publication = Publication(
            clip_id=clip.id, platform="youtube", scheduled_for=original,
            requested_for=original, timezone_name="Asia/Tbilisi",
            status=PublishStatus.scheduled, dry_run=True,
        )
        db.add(publication)
        db.commit()
        publication_id = publication.id

    monkeypatch.setattr(
        scheduler_service, "_publish",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("temporary failure")),
    )
    scheduler_service.dispatch_due()
    with SessionLocal() as db:
        saved = db.get(Publication, publication_id)
        assert saved.status == PublishStatus.scheduled
        assert saved.scheduled_for == saved.requested_for
        assert saved.next_attempt_at is not None
        assert saved.attempts == 1
