from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.database.models import (
    Clip, Publication, PublicationArchive, PublishStatus, Source,
)
from app.database.session import SessionLocal
from app.main import app


def _source_with_clips(tmp_path: Path, count: int) -> tuple[str, list[str], list[Path]]:
    with SessionLocal() as db:
        source_file = tmp_path / "source.mp4"
        source_file.write_bytes(b"source")
        source = Source(
            original_name=source_file.name, storage_path=str(source_file),
            checksum="q" * 64, size_bytes=source_file.stat().st_size,
            rights_confirmed=True,
        )
        db.add(source); db.flush()
        clip_ids: list[str] = []
        paths: list[Path] = []
        for index in range(count):
            media = tmp_path / f"clip-{index}.mp4"
            media.write_bytes(f"video-{index}".encode())
            clip = Clip(
                source_id=source.id, sequence_number=index + 1,
                title=f"Клип {index + 1}", start=index * 30, end=index * 30 + 25,
                score=100 - index, transcript=f"Самостоятельный момент {index + 1}",
                export_path=str(media), processing_status="completed",
            )
            db.add(clip); db.flush()
            clip_ids.append(clip.id); paths.append(media)
        db.commit()
        return source.id, clip_ids, paths


def test_publications_api_returns_complete_queue_without_hidden_limit(tmp_path: Path):
    _, clip_ids, _ = _source_with_clips(tmp_path, 22)
    start = datetime.now(timezone.utc) + timedelta(days=3)
    with SessionLocal() as db:
        for index, clip_id in enumerate(clip_ids):
            db.add(Publication(
                clip_id=clip_id,
                platform=("youtube", "instagram", "tiktok")[index % 3],
                scheduled_for=start + timedelta(minutes=index),
                requested_for=start + timedelta(minutes=index),
                timezone_name="Asia/Tbilisi", status=PublishStatus.scheduled,
            ))
        db.commit()
    client = TestClient(app)
    response = client.get("/api/publications")
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 22
    assert [row["scheduled_for"] for row in rows] == sorted(
        row["scheduled_for"] for row in rows
    )


def test_delete_scheduled_publication_archives_and_preserves_clip_and_mp4(tmp_path: Path):
    _, clip_ids, media_paths = _source_with_clips(tmp_path, 1)
    with SessionLocal() as db:
        publication = Publication(
            clip_id=clip_ids[0], platform="youtube",
            scheduled_for=datetime.now(timezone.utc) + timedelta(days=2),
            requested_for=datetime.now(timezone.utc) + timedelta(days=2),
            timezone_name="Asia/Tbilisi", status=PublishStatus.scheduled,
            publish_metadata={"title": "Проверка архива"},
        )
        db.add(publication); db.commit()
        publication_id = publication.id

    client = TestClient(app)
    response = client.delete(f"/api/publications/{publication_id}")
    assert response.status_code == 204, response.text
    assert client.get(f"/api/publications/{publication_id}").status_code == 404

    with SessionLocal() as db:
        assert db.get(Publication, publication_id) is None
        clip = db.get(Clip, clip_ids[0])
        assert clip is not None
        assert clip.export_path == str(media_paths[0])
        archive = db.scalar(select(PublicationArchive).where(
            PublicationArchive.publication_id == publication_id,
        ))
        assert archive is not None
        assert archive.reason == "user_removed_from_queue"
        assert archive.payload["clip_id"] == clip_ids[0]
        assert archive.payload["status"] == "scheduled"
        assert db.scalar(select(func.count(PublicationArchive.id))) == 1
    assert media_paths[0].is_file()


def test_delete_rejects_processing_and_published_publications(tmp_path: Path):
    _, clip_ids, media_paths = _source_with_clips(tmp_path, 2)
    publication_ids: list[str] = []
    with SessionLocal() as db:
        for clip_id, status in zip(
            clip_ids, (PublishStatus.processing, PublishStatus.published), strict=True,
        ):
            publication = Publication(
                clip_id=clip_id, platform="youtube",
                scheduled_for=datetime.now(timezone.utc) + timedelta(days=2),
                timezone_name="UTC", status=status,
            )
            db.add(publication); db.flush(); publication_ids.append(publication.id)
        db.commit()

    client = TestClient(app)
    for publication_id in publication_ids:
        response = client.delete(f"/api/publications/{publication_id}")
        assert response.status_code == 409
        assert "ещё не начала" in response.json()["detail"]

    with SessionLocal() as db:
        assert all(db.get(Publication, publication_id) is not None for publication_id in publication_ids)
        assert db.scalar(select(func.count(PublicationArchive.id))) == 0
        assert all(db.get(Clip, clip_id) is not None for clip_id in clip_ids)
    assert all(path.is_file() for path in media_paths)
