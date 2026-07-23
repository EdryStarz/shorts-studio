from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import threading
from typing import Callable

from sqlalchemy import func, select

from app.clip_scoring.service import assess_video_metadata, load_virality_config
from app.core.config import get_settings
from app.core.logging import log
from app.database.models import (
    ChannelVideo, Clip, Job, JobStatus, Source, StageRun,
)
from app.database.session import SessionLocal
from app.export_pipeline.pipeline import process_job
from app.ingestion.youtube_links import (
    DownloadedVideo, YouTubeVideoMetadata, download_youtube_video,
    fetch_youtube_metadata, list_channel_videos,
)


_CYCLE_LOCK = threading.Lock()


@dataclass(frozen=True)
class ChannelCycleResult:
    discovered: int = 0
    baseline_ignored: int = 0
    processed: int = 0
    metadata_rejected: int = 0
    content_rejected: int = 0
    ready_clips: int = 0
    scheduled_publications: int = 0
    errors: int = 0
    skipped_locked: bool = False


def _metadata_dict(metadata: YouTubeVideoMetadata) -> dict:
    value = asdict(metadata)
    value["published_at"] = metadata.published_at.isoformat() if metadata.published_at else None
    value["tags"] = list(metadata.tags)
    return value


def _update_metadata(item: ChannelVideo, metadata: YouTubeVideoMetadata) -> None:
    item.title = metadata.title
    item.description = metadata.description
    item.duration = metadata.duration
    item.published_at = metadata.published_at
    item.video_metadata = _metadata_dict(metadata)
    item.updated_at = datetime.now(timezone.utc)


def _register_discoveries(entries: list[YouTubeVideoMetadata]) -> tuple[int, int]:
    settings = get_settings()
    config = load_virality_config()["channel_watcher"]
    discovered = 0
    ignored = 0
    with SessionLocal() as db:
        known = int(db.scalar(select(func.count()).select_from(ChannelVideo)) or 0)
        first_run = known == 0
        for index, metadata in enumerate(entries):
            if db.get(ChannelVideo, metadata.video_id):
                continue
            process_now = not first_run or (
                index == 0 and bool(config.get("process_latest_on_first_run", True))
            )
            item = ChannelVideo(
                video_id=metadata.video_id,
                channel_url=settings.channel_watch_url,
                video_url=metadata.url,
                state="discovered" if process_now else "ignored_baseline",
            )
            _update_metadata(item, metadata)
            db.add(item)
            discovered += int(process_now)
            ignored += int(not process_now)
        db.commit()
    return discovered, ignored


def _source_for_download(db, item: ChannelVideo, downloaded: DownloadedVideo) -> Source:
    source = db.scalar(select(Source).where(Source.checksum == downloaded.checksum))
    if source is None:
        source = Source(
            original_name=downloaded.original_name,
            storage_path=str(downloaded.path),
            checksum=downloaded.checksum,
            size_bytes=downloaded.size_bytes,
            rights_confirmed=True,
        )
        db.add(source)
        db.flush()
    item.source_id = source.id
    item.state = "downloaded"
    item.error = None
    db.commit()
    return source


def _job_for_source(db, source: Source) -> Job:
    job = db.scalar(
        select(Job).where(Job.source_id == source.id).order_by(Job.created_at.desc())
    )
    if job is None or job.status == JobStatus.failed:
        job = Job(source_id=source.id)
        db.add(job)
        db.commit()
        db.refresh(job)
    return job


def _finalize_processing(video_id: str) -> tuple[bool, int]:
    with SessionLocal() as db:
        item = db.get(ChannelVideo, video_id)
        if item is None or item.source_id is None:
            return False, 0
        job = db.scalar(select(Job).where(Job.source_id == item.source_id).order_by(Job.created_at.desc()))
        filter_stage = db.scalar(
            select(StageRun).where(StageRun.job_id == job.id, StageRun.name == "filter")
        ) if job else None
        artifact = filter_stage.artifact if filter_stage and filter_stage.artifact else {}
        item.virality_score = artifact.get("score")
        accepted = bool(artifact.get("accepted")) and bool(job and job.status == JobStatus.completed)
        clip_count = int(db.scalar(
            select(func.count()).select_from(Clip).where(Clip.source_id == item.source_id)
        ) or 0)
        item.state = "ready" if accepted and clip_count else "rejected_content"
        item.error = None
        db.commit()
        return item.state == "ready", clip_count


def _mark_retry(video_id: str, error: Exception) -> None:
    with SessionLocal() as db:
        item = db.get(ChannelVideo, video_id)
        if item:
            item.state = "retry"
            item.error = str(error)[:2000]
            db.commit()


def run_channel_cycle(
    *,
    list_videos: Callable[..., list[YouTubeVideoMetadata]] = list_channel_videos,
    fetch_metadata: Callable[..., YouTubeVideoMetadata] = fetch_youtube_metadata,
    download_video: Callable[..., DownloadedVideo] = download_youtube_video,
    process: Callable[[str], None] = process_job,
    schedule_pool: Callable[..., list] | None = None,
) -> ChannelCycleResult:
    """Discover, triage, process and queue at most one new channel video by default."""
    if not _CYCLE_LOCK.acquire(blocking=False):
        return ChannelCycleResult(skipped_locked=True)
    result = ChannelCycleResult()
    settings = get_settings()
    try:
        entries = list_videos(settings.channel_watch_url, settings.channel_watch_lookback_videos)
        discovered, ignored = _register_discoveries(entries)
        result = ChannelCycleResult(discovered=discovered, baseline_ignored=ignored)
        with SessionLocal() as db:
            pending = list(db.scalars(
                select(ChannelVideo).where(ChannelVideo.state.in_(("discovered", "retry")))
                .order_by(ChannelVideo.published_at.desc(), ChannelVideo.discovered_at.desc())
                .limit(settings.channel_watch_max_videos_per_cycle)
            ).all())
            pending_ids = [item.video_id for item in pending]

        processed = metadata_rejected = content_rejected = ready_clips = errors = 0
        for video_id in pending_ids:
            try:
                with SessionLocal() as db:
                    item = db.get(ChannelVideo, video_id)
                    if item is None:
                        continue
                    metadata = fetch_metadata(item.video_url)
                    _update_metadata(item, metadata)
                    assessment = assess_video_metadata(
                        metadata.title, metadata.description, metadata.tags,
                        metadata.duration, metadata.live_status,
                    )
                    item.metadata_score = assessment.score
                    item.video_metadata = {
                        **item.video_metadata,
                        "metadata_assessment": assessment.details,
                        "metadata_threshold": assessment.threshold,
                    }
                    if not assessment.accepted:
                        item.state = "rejected_metadata"
                        db.commit()
                        metadata_rejected += 1
                        continue
                    item.state = "downloading"
                    db.commit()

                downloaded = download_video(metadata.url, settings.channel_watch_browser)
                with SessionLocal() as db:
                    item = db.get(ChannelVideo, video_id)
                    source = _source_for_download(db, item, downloaded)
                    job = _job_for_source(db, source)
                    item.state = "processing"
                    db.commit()
                    job_id = job.id
                    already_completed = job.status == JobStatus.completed
                if not already_completed:
                    process(job_id)
                ready, count = _finalize_processing(video_id)
                processed += 1
                ready_clips += count if ready else 0
                content_rejected += int(not ready)
            except Exception as exc:
                errors += 1
                _mark_retry(video_id, exc)
                log.exception("channel_video_processing_failed", video_id=video_id, error=str(exc))

        scheduled = 0
        if settings.channel_watch_auto_queue:
            try:
                if schedule_pool is None:
                    from app.api.router import schedule_autopilot_pool

                    schedule_pool = schedule_autopilot_pool
                with SessionLocal() as db:
                    scheduled = len(schedule_pool(db))
            except Exception as exc:
                log.warning("autopilot_queue_deferred", error=str(exc))
        result = ChannelCycleResult(
            discovered=result.discovered, baseline_ignored=result.baseline_ignored,
            processed=processed, metadata_rejected=metadata_rejected,
            content_rejected=content_rejected, ready_clips=ready_clips,
            scheduled_publications=scheduled, errors=errors,
        )
        log.info("channel_watch_cycle_completed", **asdict(result))
        return result
    finally:
        _CYCLE_LOCK.release()
