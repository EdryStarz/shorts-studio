from datetime import datetime, timezone
from pathlib import Path

from app.channel_watcher.service import run_channel_cycle
from app.database.models import ChannelVideo, Clip, Job, JobStatus, Source, StageRun, StageStatus
from app.database.session import SessionLocal
from app.ingestion.youtube_links import DownloadedVideo, YouTubeVideoMetadata


def _video(
    video_id: str, title: str = "Хесус реагирует на скандал", *,
    description: str = "Полный разбор новости и сильная реакция стримера.",
    tags: tuple[str, ...] = ("реакция", "новости"),
    duration: float = 3600,
) -> YouTubeVideoMetadata:
    return YouTubeVideoMetadata(
        video_id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        title=title,
        description=description,
        duration=duration,
        published_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
        tags=tags,
    )


def test_first_channel_cycle_indexes_old_videos_but_only_triages_the_latest():
    latest = _video("dQw4w9WgXcQ", "стрим", description="", tags=(), duration=60)
    older = _video("abc123def45")
    oldest = _video("zyx987wvu65")

    result = run_channel_cycle(
        list_videos=lambda *_args: [latest, older, oldest],
        fetch_metadata=lambda _url: latest,
        download_video=lambda *_args: (_ for _ in ()).throw(AssertionError("must not download")),
        schedule_pool=lambda _db: [],
    )

    assert result.discovered == 1
    assert result.baseline_ignored == 2
    assert result.metadata_rejected == 1
    with SessionLocal() as db:
        states = {item.video_id: item.state for item in db.query(ChannelVideo).all()}
    assert states == {
        "dQw4w9WgXcQ": "rejected_metadata",
        "abc123def45": "ignored_baseline",
        "zyx987wvu65": "ignored_baseline",
    }


def test_accepted_channel_video_runs_full_processing_before_becoming_ready(tmp_path: Path):
    latest = _video("dQw4w9WgXcQ")
    media = tmp_path / "stream.mp4"
    short = tmp_path / "short.mp4"
    media.write_bytes(b"source")
    short.write_bytes(b"short")

    def fake_process(job_id: str) -> None:
        with SessionLocal() as db:
            job = db.get(Job, job_id)
            job.status = JobStatus.completed
            db.add(StageRun(
                job_id=job.id, name="filter", status=StageStatus.completed,
                artifact={"accepted": True, "score": 0.72},
            ))
            db.add(Clip(
                source_id=job.source_id, start=12, end=42, score=89,
                transcript="Полная реакция на главную новость", export_path=str(short),
            ))
            db.commit()

    result = run_channel_cycle(
        list_videos=lambda *_args: [latest],
        fetch_metadata=lambda _url: latest,
        download_video=lambda *_args: DownloadedVideo(media, "stream.mp4", "a" * 64, media.stat().st_size),
        process=fake_process,
        schedule_pool=lambda _db: [],
    )

    assert result.processed == 1
    assert result.ready_clips == 1
    with SessionLocal() as db:
        item = db.get(ChannelVideo, latest.video_id)
        assert item.state == "ready"
        assert item.virality_score == 0.72
        assert item.source_id
        assert db.get(Source, item.source_id)
