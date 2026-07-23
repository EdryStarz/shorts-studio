from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from app.api.router import (
    _active_publications, _cluster_for_publication, _validate_publication_slot,
    batch_schedule_publications,
    schedule_autopilot_pool, update_publication_schedule, update_publishing_settings,
)
from app.api.schemas import (
    BatchScheduleRequest, PublicationScheduleUpdate, PublishingSettingsUpdate,
)
from app.database.models import (
    Clip, ConnectedAccount, Publication, PublishingPreference, PublishStatus, Source,
)
from app.database.session import SessionLocal, engine


def test_scheduler_ignores_legacy_publications_without_clips():
    orphan_id = "legacy-orphan-publication"
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.execute(
            """INSERT INTO publications
               (id, clip_id, platform, scheduled_for, timezone_name, status,
                dry_run, user_confirmed, attempts, error_log, publish_metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                orphan_id, "missing-clip", "youtube",
                datetime.now(timezone.utc).isoformat(), "UTC", "scheduled",
                1, 0, 0, "[]", "{}",
            ),
        )
        raw.commit()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
    finally:
        raw.close()

    with SessionLocal() as db:
        assert _active_publications(db, ["youtube"]) == []
        orphan = db.get(Publication, orphan_id)
        assert orphan is not None
        assert _cluster_for_publication(orphan) == "unknown"


def test_manual_publication_time_can_be_changed_inside_automatic_gap(tmp_path):
    anchor = (datetime.now(timezone.utc) + timedelta(days=2)).replace(
        hour=10, minute=0, second=0, microsecond=0,
    )
    with SessionLocal() as db:
        source = Source(
            original_name="editable.mp4", storage_path=str(tmp_path / "editable.mp4"),
            checksum="e" * 64, size_bytes=1, rights_confirmed=True,
        )
        db.add(source)
        db.flush()
        first_clip = Clip(
            source_id=source.id, start=0, end=25, score=90,
            transcript="роботы технологии турнир", export_path=str(tmp_path / "first.mp4"),
        )
        second_clip = Clip(
            source_id=source.id, start=30, end=55, score=80,
            transcript="банк акции экономика", export_path=str(tmp_path / "second.mp4"),
        )
        db.add_all([first_clip, second_clip])
        db.flush()
        first = Publication(
            clip_id=first_clip.id, platform="instagram", scheduled_for=anchor,
            timezone_name="UTC", status=PublishStatus.scheduled,
            publish_metadata={"topic_cluster": "технологии"},
        )
        second = Publication(
            clip_id=second_clip.id, platform="instagram",
            scheduled_for=anchor + timedelta(hours=5), timezone_name="UTC",
            status=PublishStatus.scheduled,
            publish_metadata={"topic_cluster": "экономика"},
        )
        db.add_all([first, second])
        db.commit()

        updated = update_publication_schedule(
            first.id,
            PublicationScheduleUpdate(
                scheduled_for=anchor + timedelta(hours=4, minutes=59), timezone_name="UTC",
            ),
            db,
        )
        assert updated.scheduled_for.replace(tzinfo=timezone.utc) == anchor + timedelta(hours=4, minutes=59)
        assert updated.requested_for == updated.scheduled_for


def test_automatic_slot_validation_still_enforces_window_and_150_minute_gap(tmp_path):
    anchor = (datetime.now(timezone.utc) + timedelta(days=2)).replace(
        hour=10, minute=0, second=0, microsecond=0,
    )
    with SessionLocal() as db:
        source = Source(
            original_name="auto.mp4", storage_path=str(tmp_path / "auto.mp4"),
            checksum="f" * 64, size_bytes=1, rights_confirmed=True,
        )
        db.add(source); db.flush()
        clip = Clip(
            source_id=source.id, start=0, end=20, score=90,
            transcript="технологии и реакция", export_path=str(tmp_path / "auto-clip.mp4"),
        )
        db.add(clip); db.flush()
        db.add(Publication(
            clip_id=clip.id, platform="youtube", scheduled_for=anchor,
            timezone_name="UTC", status=PublishStatus.scheduled,
            publish_metadata={"topic_cluster": "технологии"},
        ))
        db.commit()
        with pytest.raises(HTTPException) as gap_error:
            _validate_publication_slot(
                db, platform="youtube", scheduled_for=anchor + timedelta(minutes=149),
                timezone_name="UTC", cluster="новости",
            )
        assert gap_error.value.status_code == 409
        assert "150 minutes" in gap_error.value.detail
        with pytest.raises(HTTPException) as window_error:
            _validate_publication_slot(
                db, platform="instagram", scheduled_for=anchor.replace(hour=3),
                timezone_name="UTC", cluster="новости",
            )
        assert window_error.value.status_code == 409


def test_batch_dry_run_preserves_manual_spaced_slots_for_each_platform(tmp_path):
    with SessionLocal() as db:
        source = Source(
            original_name="source.mp4", storage_path=str(tmp_path / "source.mp4"),
            checksum="a" * 64, size_bytes=1, rights_confirmed=True,
        )
        db.add(source)
        db.flush()
        transcripts = (
            "Путин сказал ура. Путин по годам.",
            "Репортаж про Kyivstoner вызвал реакцию.",
            "Дуров ответил на блокировку Telegram.",
            "Главные новости дня. Новости обсуждают на стриме.",
        )
        for index, (score, transcript) in enumerate(zip((90.0, 85.0, 80.0, 75.0), transcripts)):
            media = tmp_path / f"clip-{index}.mp4"
            media.write_bytes(b"video")
            db.add(Clip(
                source_id=source.id, start=index * 30, end=index * 30 + 25,
                score=score, transcript=transcript, export_path=str(media),
                checksum=str(index) * 64,
            ))
        db.commit()
        local_start = (datetime.now(ZoneInfo("Asia/Tbilisi")) + timedelta(days=2)).replace(
            hour=10, minute=0, second=0, microsecond=0,
        )
        start = local_start.astimezone(timezone.utc)
        queued = batch_schedule_publications(BatchScheduleRequest(
            source_id=source.id, platforms=["youtube", "tiktok"],
            scheduled_from=start, interval_minutes=150,
            timezone_name="Asia/Tbilisi", dry_run=True,
        ), db)
        assert len(queued) == 8
        assert {item.platform for item in queued} == {"youtube", "tiktok"}
        for platform in ("youtube", "tiktok"):
            platform_items = sorted(
                (item for item in queued if item.platform == platform),
                key=lambda item: item.scheduled_for,
            )
            assert len(platform_items) == 4
            assert all(
                right.scheduled_for - left.scheduled_for >= timedelta(minutes=150)
                for left, right in zip(platform_items, platform_items[1:])
            )
            assert [item.scheduled_for.replace(tzinfo=timezone.utc) for item in platform_items] == [
                start + timedelta(minutes=index * 150) for index in range(4)
            ]
            assert all(item.requested_for == item.scheduled_for for item in platform_items)
            assert all(
                10 <= item.scheduled_for.replace(tzinfo=timezone.utc).astimezone(
                    ZoneInfo("Asia/Tbilisi")
                ).hour <= 23
                for item in platform_items
            )
            clusters = [item.publish_metadata["topic_cluster"] for item in platform_items]
            assert all(left != right for left, right in zip(clusters, clusters[1:]))


def test_manual_batch_allows_one_minute_interval(tmp_path):
    with SessionLocal() as db:
        source = Source(
            original_name="manual.mp4", storage_path=str(tmp_path / "manual.mp4"),
            checksum="c" * 64, size_bytes=1, rights_confirmed=True,
        )
        db.add(source); db.flush()
        transcripts = (
            "Путин обсуждает политику и выборы",
            "Kyivstoner записал голосовое и вызвал реакцию",
            "Дуров рассказал про Telegram и технологии",
            "Главные новости дня на стриме",
        )
        for index, transcript in enumerate(transcripts):
            media = tmp_path / f"manual-{index}.mp4"
            media.write_bytes(b"video")
            db.add(Clip(
                source_id=source.id, start=index * 30, end=index * 30 + 25,
                score=90 - index, transcript=transcript,
                export_path=str(media), checksum=f"m{index}".ljust(64, "0"),
            ))
        db.commit()
        start = datetime.now(timezone.utc) + timedelta(days=2)
        queued = batch_schedule_publications(BatchScheduleRequest(
            source_id=source.id, platforms=["youtube"], scheduled_from=start,
            interval_minutes=1, timezone_name="UTC", dry_run=True,
        ), db)
        ordered = sorted(queued, key=lambda item: item.scheduled_for)
        assert len(ordered) == 4
        assert all(
            right.scheduled_for - left.scheduled_for == timedelta(minutes=1)
            for left, right in zip(ordered, ordered[1:])
        )


def test_real_publishing_requires_explicit_enable_confirmation():
    with SessionLocal() as db:
        with pytest.raises(HTTPException) as exc:
            update_publishing_settings(
                PublishingSettingsUpdate(enabled=True, user_confirmed=False), db,
            )
        assert exc.value.status_code == 422
        result = update_publishing_settings(
            PublishingSettingsUpdate(enabled=True, user_confirmed=True), db,
        )
        assert result.enabled is True


def test_adaptive_batch_rejects_fewer_than_four_clips(tmp_path):
    with SessionLocal() as db:
        source = Source(
            original_name="short-source.mp4", storage_path=str(tmp_path / "short-source.mp4"),
            checksum="b" * 64, size_bytes=1, rights_confirmed=True,
        )
        db.add(source)
        db.flush()
        for index in range(3):
            media = tmp_path / f"short-{index}.mp4"
            media.write_bytes(b"video")
            db.add(Clip(
                source_id=source.id, start=index * 30, end=index * 30 + 25,
                score=90 - index, transcript=f"Тема {index} тема {index}",
                export_path=str(media), checksum=f"{index + 4}" * 64,
            ))
        db.commit()
        with pytest.raises(HTTPException) as exc:
            batch_schedule_publications(BatchScheduleRequest(
                source_id=source.id, platforms=["youtube"],
                scheduled_from=datetime.now(timezone.utc) + timedelta(hours=1),
                dry_run=True,
            ), db)
        assert exc.value.status_code == 409
        assert "4-7" in exc.value.detail


def test_autopilot_builds_one_daily_pool_across_sources_and_not_per_stream(tmp_path):
    with SessionLocal() as db:
        db.add(PublishingPreference(id=1, enabled=True))
        db.add_all([
            ConnectedAccount(platform="youtube", external_id="yt", display_name="YouTube", encrypted_credentials="x"),
            ConnectedAccount(platform="instagram", external_id="ig", display_name="Instagram", encrypted_credentials="x"),
        ])
        for source_index in range(4):
            source_file = tmp_path / f"source-{source_index}.mp4"
            source_file.write_bytes(b"source")
            source = Source(
                original_name=source_file.name, storage_path=str(source_file),
                checksum=f"{source_index + 1}" * 64, size_bytes=1, rights_confirmed=True,
            )
            db.add(source)
            db.flush()
            for clip_index in range(2):
                output = tmp_path / f"clip-{source_index}-{clip_index}.mp4"
                output.write_bytes(b"clip")
                db.add(Clip(
                    source_id=source.id, start=clip_index * 30, end=clip_index * 30 + 25,
                    score=100 - source_index * 10 - clip_index,
                    transcript=(
                        f"topic{source_index} topic{source_index} "
                        f"important reaction {clip_index}"
                    ),
                    export_path=str(output),
                ))
        db.commit()

        scheduled = schedule_autopilot_pool(
            db, scheduled_from=datetime.now(timezone.utc) + timedelta(hours=1),
        )

        assert 8 <= len(scheduled) <= 14
        selected_clip_ids = {item.clip_id for item in scheduled}
        assert 4 <= len(selected_clip_ids) <= 7
        source_ids = [db.get(Clip, clip_id).source_id for clip_id in selected_clip_ids]
        assert all(source_ids.count(source_id) <= 2 for source_id in set(source_ids))
        assert {item.platform for item in scheduled} == {"youtube", "instagram"}
