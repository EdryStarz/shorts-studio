import asyncio
import hashlib
import hmac
import html
import itertools
import json
import random
import secrets
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.schemas import (
    AccountView, BatchScheduleRequest, ClipUpdate, ClipView, JobView, OAuthAppConfigUpdate,
    OAuthAppConfigView, PublicationScheduleUpdate, PublicationView, TikTokCreatorInfoView,
    TikTokPublicationRequest,
    PublishingSettingsUpdate, PublishingSettingsView, RenderRequest, ScheduleRequest, SourceView,
    YouTubeBrowserView, YouTubeImportJobView, YouTubeImportRequest,
)
from app.core.config import get_settings
from app.clip_scoring.service import load_virality_config
from app.core.secrets import decrypt_credentials, encrypt_credentials
from app.database.models import (
    Clip, ConnectedAccount, Job, Publication, PublicationArchive, PublishingPreference,
    PublishStatus, Source, TranscriptSegment,
)
from app.database.session import get_db
from app.export_pipeline.service import render_vertical_clip
from app.media_probe.service import probe_media
from app.ingestion.service import checksum_file, persist_upload
from app.ingestion.youtube_links import (
    browser_profiles, download_youtube_video, normalize_video_url,
)
from app.platform_connectors.registry import get_publisher
from app.platform_connectors.oauth_config import (
    PLATFORMS, REQUIREMENTS, SETUP_URLS, get_oauth_config, save_oauth_config,
)
from app.profanity_filter.service import find_all_matches, mask_words
from app.publishing_metadata import build_publication_metadata, topic_cluster
from app.subtitle_renderer.service import write_ass, write_srt, write_vtt
from app.export_pipeline.pipeline import process_job
from app.tasks import process_video_task


router = APIRouter(prefix="/api")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(422, f"unknown timezone: {name}") from exc


def _clock_minutes(value: str) -> int:
    hour, minute = (int(part) for part in value.split(":", 1))
    return hour * 60 + minute


def _scheduler_window(config: dict) -> tuple[int, int]:
    return (
        _clock_minutes(str(config.get("allowed_window_start", "10:00"))),
        _clock_minutes(str(config.get("allowed_window_end", "23:00"))),
    )


def _daily_slot_capacity(config: dict) -> int:
    start, end = _scheduler_window(config)
    return 1 + max(0, end - start) // int(config["minimum_gap_minutes"])


def _cluster_for_publication(publication: Publication) -> str:
    metadata = publication.publish_metadata or {}
    if metadata.get("topic_cluster"):
        return str(metadata["topic_cluster"])
    # Old databases can contain publications whose clip was removed before
    # SQLite foreign-key enforcement was enabled.  They must not take down the
    # scheduler while the application is upgrading that database in place.
    clip = publication.clip
    if clip is None:
        return "unknown"
    return topic_cluster(clip.subtitle_text or clip.transcript)


def _active_publications(db: Session, platforms: list[str]) -> list[Publication]:
    return list(db.scalars(
        select(Publication).join(Clip, Publication.clip_id == Clip.id).where(
            Publication.platform.in_(platforms),
            Publication.status != PublishStatus.failed,
        )
    ).all())


def _same_local_day(value: datetime, reference: datetime, zone: ZoneInfo) -> bool:
    return _as_utc(value).astimezone(zone).date() == _as_utc(reference).astimezone(zone).date()


def _validate_manual_publication_time(
    scheduled_for: datetime, timezone_name: str,
) -> datetime:
    """Validate a user-selected time without applying automatic scheduling policy."""
    _timezone(timezone_name)
    normalized = _as_utc(scheduled_for)
    if normalized <= datetime.now(timezone.utc):
        raise HTTPException(422, "Время публикации должно находиться в будущем.")
    return normalized


def _validate_publication_slot(
    db: Session, *, platform: str, scheduled_for: datetime, timezone_name: str,
    cluster: str, exclude_id: str | None = None,
) -> None:
    config = load_virality_config()["scheduler"]
    zone = _timezone(timezone_name)
    scheduled_for = _as_utc(scheduled_for)
    local = scheduled_for.astimezone(zone)
    start_minute, end_minute = _scheduler_window(config)
    local_minute = local.hour * 60 + local.minute
    if not start_minute <= local_minute <= end_minute:
        raise HTTPException(
            409,
            f"Слот {local:%d.%m.%Y %H:%M} ({timezone_name}) запрещён: "
            "публикации разрешены только с 10:00 до 23:00.",
        )
    existing = [
        item for item in _active_publications(db, [platform])
        if item.id != exclude_id and _same_local_day(item.scheduled_for, scheduled_for, zone)
    ]
    daily_limit = min(int(config["maximum_posts_per_day"]), _daily_slot_capacity(config))
    if len(existing) >= daily_limit:
        raise HTTPException(
            409,
            f"На {local:%d.%m.%Y} для {platform} уже достигнут дневной лимит "
            f"{daily_limit} публикаций.",
        )
    minimum_gap = timedelta(minutes=int(config["minimum_gap_minutes"]))
    conflict = next((
        item for item in existing
        if abs(_as_utc(item.scheduled_for) - scheduled_for) < minimum_gap
    ), None)
    if conflict:
        conflict_local = _as_utc(conflict.scheduled_for).astimezone(zone)
        raise HTTPException(
            409,
            f"Слот конфликтует с публикацией {conflict.id} на "
            f"{conflict_local:%d.%m.%Y %H:%M}: минимальный интервал — "
            "150 минут (150 minutes).",
        )
    ordered = sorted(
        [*[( _as_utc(item.scheduled_for), _cluster_for_publication(item)) for item in existing],
         (scheduled_for, cluster)],
        key=lambda item: item[0],
    )
    if any(left[1] == right[1] for left, right in zip(ordered, ordered[1:], strict=False)):
        raise HTTPException(409, "Два соседних ролика относятся к одной теме; измените время или клип.")


@dataclass(frozen=True)
class PlannedSlot:
    scheduled_for: datetime
    sampled_score: float


class ContextualThompsonScheduler:
    """Local contextual bandit with research priors and fractional Beta updates."""

    def __init__(self, seed: str):
        self.config = load_virality_config()["scheduler"]
        digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        self.random = random.Random(int(digest[:16], 16))

    @staticmethod
    def _reward(publication: Publication) -> float | None:
        analytics = (publication.publish_metadata or {}).get("analytics") or {}
        if "reward" in analytics:
            return max(0.0, min(1.0, float(analytics["reward"])))
        required = (
            "completion_rate", "average_watch_ratio", "share_rate",
            "comment_rate", "view_velocity_6h",
        )
        if not all(key in analytics for key in required):
            return None
        weights = load_virality_config()["scheduler"].get("reward_weights", {
            "completion_rate": 0.35, "average_watch_ratio": 0.25,
            "share_rate": 0.20, "comment_rate": 0.10, "view_velocity_6h": 0.10,
        })
        return max(0.0, min(1.0, sum(
            float(weights[key]) * max(0.0, min(1.0, float(analytics[key])))
            for key in required
        )))

    def _prior(self, local_time: datetime) -> tuple[float, float]:
        minute = local_time.hour * 60 + local_time.minute
        exploit = {
            int(value[:2]) * 60 + int(value[3:]) for value in self.config["exploit_slots"]
        }
        explore = {
            int(value[:2]) * 60 + int(value[3:]) for value in self.config["explore_slots"]
        }
        if any(abs(minute - target) <= 30 for target in exploit):
            mean, strength = 0.70, 7.0
        elif any(abs(minute - target) <= 30 for target in explore):
            mean, strength = 0.58, 5.0
        else:
            mean, strength = 0.42, 4.0
        return 1.0 + mean * strength, 1.0 + (1.0 - mean) * strength

    def _sample_context(
        self, local_time: datetime, platform: str, topics: list[str],
        history: list[Publication], zone: ZoneInfo,
    ) -> float:
        values = []
        for topic in topics:
            alpha, beta = self._prior(local_time)
            for publication in history:
                if publication.platform != platform or _cluster_for_publication(publication) != topic:
                    continue
                published_local = _as_utc(publication.scheduled_for).astimezone(zone)
                if abs(published_local.hour * 60 + published_local.minute
                       - (local_time.hour * 60 + local_time.minute)) > 60:
                    continue
                reward = self._reward(publication)
                if reward is not None:
                    alpha += reward
                    beta += 1.0 - reward
            values.append(self.random.betavariate(alpha, beta))
        return sum(values) / max(len(values), 1)

    @staticmethod
    def _best_spaced_slots(
        candidates: list[datetime], scores: list[float], count: int, gap_minutes: int,
    ) -> list[int]:
        states: list[list[tuple[float, tuple[int, ...]] | None]] = [
            [None] * len(candidates) for _ in range(count + 1)
        ]
        for index in range(len(candidates)):
            states[1][index] = (scores[index], (index,))
        gap = timedelta(minutes=gap_minutes)
        for size in range(2, count + 1):
            for index, candidate in enumerate(candidates):
                previous = [
                    states[size - 1][prior] for prior in range(index)
                    if candidate - candidates[prior] >= gap and states[size - 1][prior] is not None
                ]
                if previous:
                    best = max(previous, key=lambda item: item[0])
                    states[size][index] = (best[0] + scores[index], (*best[1], index))
        complete = [state for state in states[count] if state is not None]
        return list(max(complete, key=lambda item: item[0])[1]) if complete else []

    def plan(
        self, *, start: datetime, timezone_name: str, platforms: list[str],
        topics: list[str], history: list[Publication],
    ) -> list[PlannedSlot]:
        zone = _timezone(timezone_name)
        start = _as_utc(start)
        local_start = start.astimezone(zone)
        count = len(topics)
        gap_minutes = int(self.config["minimum_gap_minutes"])
        maximum = min(
            int(self.config["maximum_posts_per_day"]), _daily_slot_capacity(self.config),
        )
        start_minute, end_minute = _scheduler_window(self.config)
        for day_offset in range(8):
            day = local_start.date() + timedelta(days=day_offset)
            day_history = {
                platform: [
                    item for item in history if item.platform == platform
                    and _as_utc(item.scheduled_for).astimezone(zone).date() == day
                ] for platform in platforms
            }
            if any(len(items) + count > maximum for items in day_history.values()):
                continue
            candidates: list[datetime] = []
            for minute in range(start_minute, end_minute + 1, 30):
                local = datetime(
                    day.year, day.month, day.day, minute // 60, minute % 60, tzinfo=zone,
                )
                utc = local.astimezone(timezone.utc)
                if utc < start:
                    continue
                if any(
                    abs(utc - _as_utc(item.scheduled_for)) < timedelta(minutes=gap_minutes)
                    for items in day_history.values() for item in items
                ):
                    continue
                candidates.append(utc)
            scores = [
                sum(
                    self._sample_context(value.astimezone(zone), platform, topics, history, zone)
                    for platform in platforms
                ) / len(platforms)
                for value in candidates
            ]
            indexes = self._best_spaced_slots(candidates, scores, count, gap_minutes)
            if indexes:
                return [PlannedSlot(candidates[index], round(scores[index], 4)) for index in indexes]
        raise HTTPException(409, "no day can fit the adaptive 4-7 post schedule")


def _order_clips_for_slots(
    clips: list[Clip], slots: list[PlannedSlot], history: list[Publication],
    platforms: list[str], timezone_name: str,
) -> list[Clip]:
    zone = _timezone(timezone_name)
    for indexes in itertools.permutations(range(len(clips))):
        ordered = [clips[index] for index in indexes]
        topics = [topic_cluster(clip.subtitle_text or clip.transcript) for clip in ordered]
        if any(left == right for left, right in zip(topics, topics[1:], strict=False)):
            continue
        valid = True
        for platform in platforms:
            timeline = [
                (_as_utc(item.scheduled_for), _cluster_for_publication(item))
                for item in history if item.platform == platform
                and _same_local_day(item.scheduled_for, slots[0].scheduled_for, zone)
            ]
            timeline.extend(
                (slot.scheduled_for, topic) for slot, topic in zip(slots, topics, strict=True)
            )
            timeline.sort(key=lambda item: item[0])
            if any(left[1] == right[1] for left, right in zip(timeline, timeline[1:], strict=False)):
                valid = False
                break
        if valid:
            return ordered
    raise HTTPException(409, "not enough topic diversity for a non-repeating daily schedule")


def _not_found(kind: str) -> HTTPException:
    return HTTPException(404, f"{kind} not found")


def _words_for_range(
    db: Session, source_id: str, start: float, end: float,
) -> list[dict]:
    """Return transcript words belonging to the edited clip range.

    Clip boundaries can be changed after automatic scoring.  Reusing the old
    word list would leave captions at the previous candidate's timestamps, so
    every boundary edit must rebuild the list from the source transcript.
    """
    segments = db.scalars(
        select(TranscriptSegment).where(
            TranscriptSegment.source_id == source_id,
            TranscriptSegment.end > start,
            TranscriptSegment.start < end,
        ).order_by(TranscriptSegment.start)
    ).all()
    words: list[dict] = []
    for segment in segments:
        for raw_word in segment.words or []:
            word = dict(raw_word)
            word_start = float(word.get("start", segment.start))
            word_end = float(word.get("end", segment.end))
            if word_end > start and word_start < end:
                words.append(word)
    return words


def _ensure_clip_metadata(clips: list[Clip], db: Session) -> list[Clip]:
    """Persist stable display metadata for old and newly generated clips."""
    changed = False
    numbers = [clip.sequence_number for clip in clips]
    renumber = any(number <= 0 for number in numbers) or len(set(numbers)) != len(numbers)
    for index, clip in enumerate(clips, 1):
        if renumber and clip.sequence_number != index:
            clip.sequence_number = index
            changed = True
        technical_title = (
            not clip.title
            or clip.title.casefold() in {"clip.mp4", "output.mp4", "short.mp4", "mp4"}
        )
        if technical_title or not clip.description or not 5 <= len(clip.hashtags or []) <= 8:
            metadata = build_publication_metadata(
                clip.subtitle_text or clip.transcript, "youtube",
            )
            clip.title = metadata["display_title"]
            clip.description = metadata["description"]
            clip.hashtags = metadata["tags"]
            changed = True
        expected_status = (
            "completed" if clip.export_path and Path(clip.export_path).is_file()
            else "processing"
        )
        if clip.processing_status != expected_status:
            clip.processing_status = expected_status
            changed = True
        if expected_status == "completed" and clip.preview_path != clip.export_path:
            clip.preview_path = clip.export_path
            changed = True
    if changed:
        db.commit()
    return clips


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "real_publishing_enabled": get_settings().real_publishing_enabled}


@router.get("/youtube/browsers", response_model=list[YouTubeBrowserView])
def youtube_browsers() -> list[YouTubeBrowserView]:
    return [YouTubeBrowserView(**profile.__dict__) for profile in browser_profiles()]


@router.post("/youtube/import", response_model=SourceView, status_code=201)
async def import_youtube_source(
    payload: YouTubeImportRequest, db: Session = Depends(get_db),
) -> Source:
    if not payload.rights_confirmed:
        raise HTTPException(422, "you must confirm rights to process and republish this content")
    if payload.adult_content and not payload.adult_access_confirmed:
        raise HTTPException(422, "adult content requires confirmed lawful adult access")
    try:
        normalized_url = normalize_video_url(payload.url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    existing_by_url = db.scalar(select(Source).where(Source.source_url == normalized_url))
    if existing_by_url and Path(existing_by_url.storage_path).is_file():
        existing_by_url.rights_confirmed = True
        existing_by_url.adult_content = payload.adult_content
        existing_by_url.adult_access_confirmed = payload.adult_access_confirmed
        db.commit()
        db.refresh(existing_by_url)
        return existing_by_url

    try:
        import_timeout = int(getattr(get_settings(), "link_import_timeout_seconds", 1800))
        downloaded = await asyncio.wait_for(
            run_in_threadpool(download_youtube_video, normalized_url, payload.browser),
            timeout=import_timeout,
        )
    except TimeoutError as exc:
        raise HTTPException(
            504,
            "Импорт превысил допустимое время. Повторите запрос: частично скачанный "
            "файл сохранён, и загрузка продолжится с последнего полученного байта.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Локальный импорт завершился ошибкой: {str(exc)[:300]}") from exc

    existing = db.scalar(select(Source).where(Source.checksum == downloaded.checksum))
    if existing:
        existing.source_url = downloaded.source_url or normalized_url
        existing.title = downloaded.title or existing.title or existing.original_name
        existing.author = downloaded.author or existing.author
        existing.duration = downloaded.duration or existing.duration
        existing.import_status = "completed" if existing.clips else "processing"
        existing.import_error = None
        db.commit()
        db.refresh(existing)
        return existing
    source = Source(
        original_name=downloaded.original_name,
        source_url=downloaded.source_url or normalized_url,
        title=downloaded.title or Path(downloaded.original_name).stem,
        author=downloaded.author,
        import_status="processing",
        import_error=None,
        storage_path=str(downloaded.path),
        checksum=downloaded.checksum,
        size_bytes=downloaded.size_bytes,
        duration=downloaded.duration,
        rights_confirmed=True,
        adult_content=payload.adult_content,
        adult_access_confirmed=payload.adult_access_confirmed,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


def _start_or_resume_processing(source: Source, db: Session) -> Job:
    """Return the active job or durably create and dispatch a new one."""
    active = db.scalar(select(Job).where(Job.source_id == source.id).order_by(Job.created_at.desc()))
    if active and active.status.value in {"queued", "running"}:
        return active
    if active and active.status.value == "completed":
        source.import_status = "completed"
        source.import_error = None
        db.commit()
        return active
    source.import_status = "processing"
    source.import_error = None
    job = Job(source_id=source.id)
    db.add(job)
    db.commit()
    db.refresh(job)
    if get_settings().sync_processing:
        threading.Thread(
            target=process_job,
            args=(job.id,),
            daemon=True,
            name=f"process-{job.id}",
        ).start()
    else:
        process_video_task.delay(job.id)
    return job


@router.post("/youtube/import-and-process", response_model=YouTubeImportJobView, status_code=202)
async def import_youtube_and_process(
    payload: YouTubeImportRequest, db: Session = Depends(get_db),
) -> YouTubeImportJobView:
    """Import and queue analysis in one request so the UI cannot lose the hand-off."""
    source = await import_youtube_source(payload, db)
    job = _start_or_resume_processing(source, db)
    return YouTubeImportJobView(
        source=SourceView.model_validate(source),
        job=JobView.model_validate(job),
    )


@router.post("/sources", response_model=SourceView, status_code=201)
async def upload_source(
    file: UploadFile = File(...), rights_confirmed: bool = Form(...), adult_content: bool = Form(False),
    adult_access_confirmed: bool = Form(False), db: Session = Depends(get_db),
) -> Source:
    if not rights_confirmed:
        raise HTTPException(422, "you must confirm rights to process and republish this content")
    if adult_content and not adult_access_confirmed:
        raise HTTPException(422, "adult content requires confirmed lawful adult access")
    try:
        path, checksum, size = await persist_upload(file)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    existing = db.scalar(select(Source).where(Source.checksum == checksum))
    if existing:
        return existing
    source = Source(
        original_name=file.filename or path.name, storage_path=str(path), checksum=checksum, size_bytes=size,
        rights_confirmed=True, adult_content=adult_content, adult_access_confirmed=adult_access_confirmed,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


@router.get("/sources", response_model=list[SourceView])
def list_sources(db: Session = Depends(get_db)) -> list[Source]:
    return list(db.scalars(select(Source).order_by(Source.created_at.desc())).all())


@router.post("/sources/{source_id}/process", response_model=JobView, status_code=202)
def process_source(source_id: str, db: Session = Depends(get_db)) -> Job:
    source = db.get(Source, source_id)
    if not source:
        raise _not_found("source")
    return _start_or_resume_processing(source, db)


@router.get("/sources/{source_id}/latest-job", response_model=JobView | None)
def latest_source_job(source_id: str, db: Session = Depends(get_db)) -> Job | None:
    if not db.get(Source, source_id):
        raise _not_found("source")
    return db.scalar(select(Job).where(Job.source_id == source_id).order_by(Job.created_at.desc()))


@router.get("/jobs/{job_id}", response_model=JobView)
def get_job(job_id: str, db: Session = Depends(get_db)) -> Job:
    job = db.get(Job, job_id)
    if not job:
        raise _not_found("job")
    return job


@router.get("/sources/{source_id}/clips", response_model=list[ClipView])
def list_clips(source_id: str, db: Session = Depends(get_db)) -> list[Clip]:
    clips = list(db.scalars(
        select(Clip).where(Clip.source_id == source_id).order_by(Clip.score.desc())
    ).all())
    return _ensure_clip_metadata(clips, db)


@router.patch("/clips/{clip_id}", response_model=ClipView)
def update_clip(clip_id: str, payload: ClipUpdate, db: Session = Depends(get_db)) -> Clip:
    clip = db.get(Clip, clip_id)
    if not clip:
        raise _not_found("clip")
    source = db.get(Source, clip.source_id)
    if source and source.duration and payload.end > source.duration:
        raise HTTPException(422, "clip end exceeds source duration")
    clip.start, clip.end, clip.subtitle_text = payload.start, payload.end, payload.subtitle_text
    clip.words = _words_for_range(db, clip.source_id, payload.start, payload.end)
    clip.transcript = " ".join(str(word.get("word", "")) for word in clip.words).strip()
    metadata = build_publication_metadata(
        clip.subtitle_text or clip.transcript, "youtube",
    )
    clip.title = metadata["display_title"]
    clip.description = metadata["description"]
    clip.hashtags = metadata["tags"]
    clip.export_path = None
    clip.preview_path = None
    clip.processing_status = "processing"
    clip.checksum = None
    db.commit()
    db.refresh(clip)
    return clip


@router.post("/clips/{clip_id}/render", response_model=ClipView)
def render_clip(clip_id: str, payload: RenderRequest, db: Session = Depends(get_db)) -> Clip:
    settings = get_settings()
    clip = db.get(Clip, clip_id)
    if not clip:
        raise _not_found("clip")
    source = db.get(Source, clip.source_id)
    if not source or not source.width or not source.height:
        raise HTTPException(409, "source has not been probed")
    output_dir = settings.export_dir / source.id / clip.id
    output_dir.mkdir(parents=True, exist_ok=True)
    words = [dict(word) for word in clip.words]
    if clip.subtitle_text.strip() and words:
        replacement = clip.subtitle_text.split()
        for index, word in enumerate(words):
            if index < len(replacement):
                word["word"] = replacement[index]
    subtitle_words = mask_words(words, find_all_matches(words))
    ass = write_ass(
        subtitle_words, output_dir / "captions.ass", clip.start,
        payload.dynamic_subtitles,
    )
    write_srt(subtitle_words, output_dir / "captions.srt", clip.start)
    write_vtt(subtitle_words, output_dir / "captions.vtt", clip.start)
    matches = find_all_matches(clip.words)
    media_info = probe_media(Path(source.storage_path))
    clip.processing_status = "processing"
    db.commit()
    try:
        output = render_vertical_clip(
            source=Path(source.storage_path), destination=output_dir / "short.mp4", subtitle_path=ass,
            start=clip.start, end=clip.end, source_width=source.width, source_height=source.height,
            profanity_matches=matches, mode=payload.mode, has_audio=bool(source.has_audio),
            source_fps=media_info.fps,
        )
        rendered_info = probe_media(output)
        if (rendered_info.width, rendered_info.height) != (1080, 1920) or rendered_info.duration <= 0:
            raise ValueError("готовый MP4 не прошёл проверку 1080×1920 и воспроизводимости")
    except Exception as exc:
        clip.processing_status = "failed"
        db.commit()
        raise HTTPException(422, f"Не удалось собрать клип: {str(exc)[:500]}") from exc
    clip.export_path = str(output.resolve())
    clip.preview_path = str(output.resolve())
    clip.subtitle_path = str(ass.resolve())
    clip.checksum = checksum_file(output)
    clip.processing_status = "completed"
    db.commit()
    db.refresh(clip)
    return clip


@router.get("/clips/{clip_id}/download")
def download_clip(clip_id: str, db: Session = Depends(get_db)) -> FileResponse:
    clip = db.get(Clip, clip_id)
    if not clip or not clip.export_path or not Path(clip.export_path).is_file():
        raise _not_found("rendered clip")
    return FileResponse(clip.export_path, media_type="video/mp4", filename=f"short-{clip.id}.mp4")


@router.get("/clips/{clip_id}/preview")
def preview_clip(clip_id: str, db: Session = Depends(get_db)) -> FileResponse:
    clip = db.get(Clip, clip_id)
    if not clip or not clip.export_path or not Path(clip.export_path).is_file():
        raise _not_found("rendered clip")
    return FileResponse(clip.export_path, media_type="video/mp4")


@router.delete("/sources/{source_id}", status_code=204)
def delete_source(source_id: str, db: Session = Depends(get_db)) -> None:
    settings = get_settings()
    source = db.get(Source, source_id)
    if not source:
        raise _not_found("source")
    allowed_root = settings.storage_root.resolve()
    paths = [Path(source.storage_path), settings.work_dir / source.id, settings.export_dir / source.id]
    db.delete(source)
    db.commit()
    for path in paths:
        resolved = path.resolve()
        if allowed_root == resolved or allowed_root not in resolved.parents:
            continue
        if resolved.is_dir():
            shutil.rmtree(resolved)
        elif resolved.is_file():
            resolved.unlink()


@router.post("/publications", response_model=PublicationView, status_code=201)
def schedule_publication(payload: ScheduleRequest, db: Session = Depends(get_db)) -> Publication:
    clip = db.get(Clip, payload.clip_id)
    if not clip or not clip.export_path:
        raise HTTPException(409, "render the clip before scheduling")
    if not payload.dry_run and not payload.user_confirmed:
        raise HTTPException(422, "real publication requires explicit confirmation")
    if not payload.dry_run and not payload.account_id:
        raise HTTPException(422, "real publication requires a connected account")
    if clip.checksum:
        duplicate = db.scalar(
            select(Publication).join(Clip).where(
                Clip.checksum == clip.checksum, Publication.platform == payload.platform,
                Publication.platform_post_id.is_not(None),
            )
        )
        if duplicate:
            raise HTTPException(409, "this rendered file has already been published to that platform")
    scheduled_for = _validate_manual_publication_time(
        payload.scheduled_for, payload.timezone_name,
    )
    metadata = build_publication_metadata(
        f"{clip.title}. {clip.subtitle_text or clip.transcript}", payload.platform,
    )
    publication = Publication(
        **payload.model_dump(exclude={"scheduled_for"}), scheduled_for=scheduled_for,
        requested_for=scheduled_for, publish_metadata=metadata,
    )
    db.add(publication)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(409, "this clip is already scheduled for that platform") from exc
    db.refresh(publication)
    return publication


@router.get("/publications", response_model=list[PublicationView])
def list_publications(db: Session = Depends(get_db)) -> list[Publication]:
    return list(db.scalars(select(Publication).order_by(Publication.scheduled_for.asc())).all())


@router.get("/publications/{publication_id}", response_model=PublicationView)
def get_publication(publication_id: str, db: Session = Depends(get_db)) -> Publication:
    publication = db.get(Publication, publication_id)
    if not publication:
        raise _not_found("publication")
    return publication


@router.patch("/publications/{publication_id}/schedule", response_model=PublicationView)
def update_publication_schedule(
    publication_id: str, payload: PublicationScheduleUpdate,
    db: Session = Depends(get_db),
) -> Publication:
    publication = db.get(Publication, publication_id)
    if not publication:
        raise _not_found("publication")
    if publication.status != PublishStatus.scheduled:
        raise HTTPException(409, "only scheduled publications can be moved")
    scheduled_for = _validate_manual_publication_time(
        payload.scheduled_for, payload.timezone_name,
    )
    publication.scheduled_for = scheduled_for
    publication.requested_for = scheduled_for
    publication.next_attempt_at = None
    publication.timezone_name = payload.timezone_name
    db.commit()
    db.refresh(publication)
    return publication


def _publication_archive_payload(publication: Publication) -> dict:
    return {
        "id": publication.id,
        "clip_id": publication.clip_id,
        "account_id": publication.account_id,
        "platform": publication.platform,
        "scheduled_for": _as_utc(publication.scheduled_for).isoformat(),
        "requested_for": (
            _as_utc(publication.requested_for).isoformat()
            if publication.requested_for else None
        ),
        "next_attempt_at": (
            _as_utc(publication.next_attempt_at).isoformat()
            if publication.next_attempt_at else None
        ),
        "timezone_name": publication.timezone_name,
        "status": publication.status.value,
        "dry_run": publication.dry_run,
        "user_confirmed": publication.user_confirmed,
        "platform_post_id": publication.platform_post_id,
        "attempts": publication.attempts,
        "error_log": publication.error_log,
        "publish_metadata": publication.publish_metadata,
    }


@router.delete("/publications/{publication_id}", status_code=204)
def delete_publication(publication_id: str, db: Session = Depends(get_db)) -> None:
    publication = db.get(Publication, publication_id)
    if not publication:
        raise _not_found("publication")
    if publication.status != PublishStatus.scheduled:
        raise HTTPException(
            409,
            "Удалить можно только запланированную публикацию, которая ещё не начала отправку.",
        )
    db.add(PublicationArchive(
        publication_id=publication.id,
        reason="user_removed_from_queue",
        payload=_publication_archive_payload(publication),
    ))
    db.delete(publication)
    db.commit()


@router.post("/publications/{publication_id}/requeue", response_model=PublicationView)
def requeue_publication(
    publication_id: str, payload: PublicationScheduleUpdate,
    db: Session = Depends(get_db),
) -> Publication:
    publication = db.get(Publication, publication_id)
    if not publication:
        raise _not_found("publication")
    if publication.status != PublishStatus.failed:
        raise HTTPException(409, "Повторно поставить в очередь можно только ошибочную публикацию.")
    clip = db.get(Clip, publication.clip_id)
    if not clip or not clip.export_path or not Path(clip.export_path).is_file():
        raise HTTPException(409, "Готовый MP4 отсутствует; сначала перерендерите клип.")
    scheduled_for = _validate_manual_publication_time(
        payload.scheduled_for, payload.timezone_name,
    )
    publication.scheduled_for = scheduled_for
    publication.requested_for = scheduled_for
    publication.next_attempt_at = None
    publication.timezone_name = payload.timezone_name
    publication.status = PublishStatus.scheduled
    publication.attempts = 0
    db.commit()
    db.refresh(publication)
    return publication


def _tiktok_creator(account_id: str, db: Session) -> tuple[ConnectedAccount, dict]:
    account = db.get(ConnectedAccount, account_id)
    if not account or account.platform != "tiktok" or account.revoked:
        raise HTTPException(404, "connected TikTok account not found")
    publisher = get_publisher("tiktok")
    credentials = decrypt_credentials(account.encrypted_credentials)
    refreshed = publisher.refresh_credentials(credentials)
    if refreshed != credentials:
        account.encrypted_credentials = encrypt_credentials(refreshed)
        db.commit()
    try:
        creator = publisher.validate_account(refreshed)
    except Exception as exc:
        raise HTTPException(502, f"TikTok creator information is unavailable: {str(exc)[:300]}") from exc
    return account, creator


@router.get("/tiktok/creator-info/{account_id}", response_model=TikTokCreatorInfoView)
def tiktok_creator_info(account_id: str, db: Session = Depends(get_db)) -> dict:
    _, creator = _tiktok_creator(account_id, db)
    return creator


@router.post("/tiktok/publications", response_model=PublicationView, status_code=201)
def schedule_tiktok_publication(
    payload: TikTokPublicationRequest, db: Session = Depends(get_db),
) -> Publication:
    preference = db.get(PublishingPreference, 1)
    if not preference or not preference.enabled:
        raise HTTPException(409, "real publishing is disabled")
    clip = db.get(Clip, payload.clip_id)
    if not clip or not clip.export_path or not Path(clip.export_path).is_file():
        raise HTTPException(409, "render the clip before publishing")
    account, creator = _tiktok_creator(payload.account_id, db)
    options = creator.get("privacy_level_options", [])
    if payload.privacy_level not in options:
        raise HTTPException(422, "selected privacy option is not currently available for this creator")
    if clip.end - clip.start > float(creator.get("max_video_post_duration_sec", 0)):
        raise HTTPException(422, "clip exceeds this creator's current TikTok duration limit")
    if payload.allow_comment and creator.get("comment_disabled"):
        raise HTTPException(422, "comments are disabled in this creator's TikTok settings")
    if payload.allow_duet and creator.get("duet_disabled"):
        raise HTTPException(422, "duet is disabled in this creator's TikTok settings")
    if payload.allow_stitch and creator.get("stitch_disabled"):
        raise HTTPException(422, "stitch is disabled in this creator's TikTok settings")
    scheduled_for = _validate_manual_publication_time(
        payload.scheduled_for, payload.timezone_name,
    )
    metadata = {
        **build_publication_metadata(
            f"{clip.title}. {clip.subtitle_text or clip.transcript}", "tiktok",
        ),
        "title": payload.title,
        "privacy": payload.privacy_level,
        "allow_comment": payload.allow_comment,
        "allow_duet": payload.allow_duet,
        "allow_stitch": payload.allow_stitch,
        "brand_content_toggle": payload.branded_content,
        "brand_organic_toggle": payload.your_brand,
        "music_usage_confirmed": True,
    }
    publication = db.scalar(select(Publication).where(
        Publication.clip_id == clip.id, Publication.platform == "tiktok",
    ))
    if publication and publication.platform_post_id:
        raise HTTPException(409, "this clip has already been published to TikTok")
    if publication is None:
        publication = Publication(clip_id=clip.id, platform="tiktok")
    publication.account_id = account.id
    publication.scheduled_for = scheduled_for
    publication.requested_for = scheduled_for
    publication.next_attempt_at = None
    publication.timezone_name = payload.timezone_name
    publication.dry_run = False
    publication.user_confirmed = True
    publication.publish_metadata = metadata
    publication.status = PublishStatus.scheduled
    publication.attempts = 0
    publication.error_log = []
    db.add(publication)
    db.commit()
    db.refresh(publication)
    return publication


def _platforms_configured(db: Session) -> dict[str, bool]:
    return {platform: get_oauth_config(platform, db).configured for platform in PLATFORMS}


@router.get("/publishing/settings", response_model=PublishingSettingsView)
def publishing_settings(db: Session = Depends(get_db)) -> PublishingSettingsView:
    preference = db.get(PublishingPreference, 1)
    return PublishingSettingsView(
        enabled=bool(preference and preference.enabled),
        platforms_configured=_platforms_configured(db),
    )


@router.put("/publishing/settings", response_model=PublishingSettingsView)
def update_publishing_settings(
    payload: PublishingSettingsUpdate, db: Session = Depends(get_db),
) -> PublishingSettingsView:
    if payload.enabled and not payload.user_confirmed:
        raise HTTPException(422, "explicit confirmation is required to enable real publishing")
    preference = db.get(PublishingPreference, 1) or PublishingPreference(id=1)
    preference.enabled = payload.enabled
    preference.confirmed_at = datetime.now(timezone.utc) if payload.enabled else None
    db.add(preference)
    db.commit()
    return PublishingSettingsView(
        enabled=preference.enabled,
        platforms_configured=_platforms_configured(db),
    )


def _oauth_config_view(platform: str, db: Session) -> OAuthAppConfigView:
    config = get_oauth_config(platform, db)
    return OAuthAppConfigView(
        platform=platform, client_id=config.client_id,
        secret_configured=bool(config.client_secret), configured=config.configured,
        redirect_uri=config.redirect_uri, setup_url=SETUP_URLS[platform],
        requirements=REQUIREMENTS[platform],
    )


@router.get("/oauth/config", response_model=list[OAuthAppConfigView])
def oauth_configs(db: Session = Depends(get_db)) -> list[OAuthAppConfigView]:
    return [_oauth_config_view(platform, db) for platform in PLATFORMS]


@router.put("/oauth/{platform}/config", response_model=OAuthAppConfigView)
def update_oauth_config(
    platform: str, payload: OAuthAppConfigUpdate, db: Session = Depends(get_db),
) -> OAuthAppConfigView:
    if platform not in PLATFORMS:
        raise HTTPException(404, "unsupported platform")
    save_oauth_config(db, platform, payload.client_id, payload.client_secret)
    return _oauth_config_view(platform, db)


@router.post("/publications/batch", response_model=list[PublicationView], status_code=201)
def batch_schedule_publications(
    payload: BatchScheduleRequest, db: Session = Depends(get_db),
) -> list[Publication]:
    source = db.get(Source, payload.source_id)
    if not source:
        raise _not_found("source")
    clips = list(db.scalars(
        select(Clip).where(Clip.source_id == source.id, Clip.export_path.is_not(None))
        .order_by(Clip.score.desc())
    ).all())
    if not clips:
        raise HTTPException(409, "render clips before creating an automatic publishing queue")
    existing_pairs = set(db.execute(
        select(Publication.clip_id, Publication.platform).where(
            Publication.platform.in_(payload.platforms),
        )
    ).all())
    clips = [
        clip for clip in clips
        if all((clip.id, platform) not in existing_pairs for platform in payload.platforms)
    ]
    if not payload.dry_run:
        if "tiktok" in payload.platforms:
            raise HTTPException(
                422,
                "TikTok real posts must use the creator review screen so privacy and interactions are selected explicitly",
            )
        preference = db.get(PublishingPreference, 1)
        if not preference or not preference.enabled:
            raise HTTPException(409, "real publishing is disabled")
        if not payload.user_confirmed:
            raise HTTPException(422, "explicit user confirmation is required")
        configured = _platforms_configured(db)
        missing = [platform for platform in payload.platforms if not configured[platform]]
        if missing:
            raise HTTPException(409, f"platform application credentials are missing: {', '.join(missing)}")

    scheduler_config = load_virality_config()["scheduler"]
    minimum = int(scheduler_config["minimum_posts_per_day"])
    maximum = min(
        int(scheduler_config["maximum_posts_per_day"]),
        _daily_slot_capacity(scheduler_config),
    )
    selected: list[Clip] = []
    for desired in range(min(maximum, len(clips)), minimum - 1, -1):
        cluster_limit = (desired + 1) // 2
        counts: dict[str, int] = {}
        candidate_selection: list[Clip] = []
        for clip in clips:
            cluster = topic_cluster(clip.subtitle_text or clip.transcript)
            if counts.get(cluster, 0) >= cluster_limit:
                continue
            candidate_selection.append(clip)
            counts[cluster] = counts.get(cluster, 0) + 1
            if len(candidate_selection) == desired:
                break
        if len(candidate_selection) == desired:
            selected = candidate_selection
            break
    if len(selected) < minimum:
        raise HTTPException(
            409,
            "automatic publishing requires 4-7 rendered clips with enough topic diversity",
        )

    for platform in payload.platforms:
        account_id = payload.account_ids.get(platform)
        if not payload.dry_run:
            account = db.get(ConnectedAccount, account_id) if account_id else None
            if not account or account.platform != platform or account.revoked:
                raise HTTPException(409, f"connect a valid {platform} account first")

    scheduled_from = _validate_manual_publication_time(
        payload.scheduled_from, payload.timezone_name,
    )
    # Manual queueing must preserve the exact start and interval selected by
    # the user. Adaptive slot selection is reserved for autonomous channel
    # watching; this endpoint rejects invalid slots instead of moving them.
    slots = [
        PlannedSlot(
            scheduled_for=scheduled_from + timedelta(
                minutes=index * payload.interval_minutes,
            ),
            sampled_score=0.0,
        )
        for index in range(len(selected))
    ]
    created: list[Publication] = []
    try:
        for clip, slot in zip(selected, slots, strict=True):
            for platform in payload.platforms:
                account_id = payload.account_ids.get(platform)
                metadata = build_publication_metadata(
                    f"{clip.title}. {clip.subtitle_text or clip.transcript}", platform,
                )
                _validate_manual_publication_time(
                    slot.scheduled_for, payload.timezone_name,
                )
                metadata["scheduler_context"] = {
                    "model": "manual-exact-slots",
                    "sampled_score": slot.sampled_score,
                    "topic_cluster": metadata["topic_cluster"],
                    "config_version": "shorts-studio-virality-v1",
                }
                publication = Publication(
                    clip_id=clip.id,
                    platform=platform,
                    account_id=account_id,
                    scheduled_for=slot.scheduled_for,
                    requested_for=slot.scheduled_for,
                    timezone_name=payload.timezone_name,
                    dry_run=payload.dry_run,
                    user_confirmed=payload.user_confirmed,
                    publish_metadata=metadata,
                )
                db.add(publication)
                db.flush()
                created.append(publication)
        db.commit()
    except Exception:
        db.rollback()
        raise
    for publication in created:
        db.refresh(publication)
    return created


def schedule_autopilot_pool(
    db: Session, *, scheduled_from: datetime | None = None,
    timezone_name: str | None = None,
) -> list[Publication]:
    """Schedule the best 4-7 ready clips across all accepted source videos."""
    settings = get_settings()
    preference = db.get(PublishingPreference, 1)
    if not preference or not preference.enabled:
        return []
    accounts = list(db.scalars(
        select(ConnectedAccount).where(ConnectedAccount.revoked.is_(False))
        .order_by(ConnectedAccount.created_at.desc())
    ).all())
    account_ids: dict[str, str] = {}
    for account in accounts:
        account_ids.setdefault(account.platform, account.id)
    platforms = [platform for platform in ("youtube", "instagram") if platform in account_ids]
    if settings.channel_watch_auto_publish_tiktok and "tiktok" in account_ids:
        platforms.append("tiktok")
    if not platforms:
        return []

    clips = list(db.scalars(
        select(Clip).where(Clip.export_path.is_not(None)).order_by(Clip.score.desc())
    ).all())
    existing_pairs = {
        (clip_id, platform) for clip_id, platform in db.execute(
            select(Publication.clip_id, Publication.platform).where(
                Publication.platform.in_(platforms),
            )
        ).all()
    }
    clips = [
        clip for clip in clips
        if all((clip.id, platform) not in existing_pairs for platform in platforms)
    ]
    scheduler_config = load_virality_config()["scheduler"]
    timezone_name = timezone_name or str(scheduler_config.get("timezone", "Asia/Tbilisi"))
    portfolio_config = load_virality_config()["portfolio"]
    minimum = int(scheduler_config["minimum_posts_per_day"])
    maximum = min(
        int(scheduler_config["maximum_posts_per_day"]),
        _daily_slot_capacity(scheduler_config),
    )
    source_limit = int(portfolio_config["maximum_clips_from_one_source_per_day"])
    selected: list[Clip] = []
    for desired in range(min(maximum, len(clips)), minimum - 1, -1):
        cluster_limit = (desired + 1) // 2
        source_counts: dict[str, int] = {}
        cluster_counts: dict[str, int] = {}
        candidate_selection: list[Clip] = []
        for clip in clips:
            cluster = topic_cluster(clip.subtitle_text or clip.transcript)
            if source_counts.get(clip.source_id, 0) >= source_limit:
                continue
            if cluster_counts.get(cluster, 0) >= cluster_limit:
                continue
            candidate_selection.append(clip)
            source_counts[clip.source_id] = source_counts.get(clip.source_id, 0) + 1
            cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1
            if len(candidate_selection) == desired:
                break
        if len(candidate_selection) == desired:
            selected = candidate_selection
            break
    if len(selected) < minimum:
        return []

    scheduled_from = _as_utc(scheduled_from or datetime.now(timezone.utc) + timedelta(minutes=15))
    history = _active_publications(db, platforms)
    topics = [topic_cluster(clip.subtitle_text or clip.transcript) for clip in selected]
    planner = ContextualThompsonScheduler(
        f"autopilot:{scheduled_from.date().isoformat()}:{','.join(platforms)}",
    )
    slots = planner.plan(
        start=scheduled_from, timezone_name=timezone_name,
        platforms=platforms, topics=topics, history=history,
    )
    selected = _order_clips_for_slots(selected, slots, history, platforms, timezone_name)
    created: list[Publication] = []
    for clip, slot in zip(selected, slots, strict=True):
        for platform in platforms:
            metadata = build_publication_metadata(
                f"{clip.title}. {clip.subtitle_text or clip.transcript}", platform,
            )
            metadata["scheduler_context"] = {
                "model": scheduler_config["model"],
                "sampled_score": slot.sampled_score,
                "topic_cluster": metadata["topic_cluster"],
                "config_version": "shorts-studio-virality-v1",
                "autopilot": True,
            }
            _validate_publication_slot(
                db, platform=platform, scheduled_for=slot.scheduled_for,
                timezone_name=timezone_name, cluster=metadata["topic_cluster"],
            )
            publication = Publication(
                clip_id=clip.id, platform=platform, account_id=account_ids[platform],
                scheduled_for=slot.scheduled_for, requested_for=slot.scheduled_for,
                timezone_name=timezone_name,
                dry_run=False, user_confirmed=True, publish_metadata=metadata,
            )
            db.add(publication)
            created.append(publication)
    db.commit()
    for publication in created:
        db.refresh(publication)
    return created


def _state(platform: str) -> str:
    key = get_settings().token_encryption_key.encode() or b"development-only-state-key"
    value = f"{platform}.{int(time.time())}.{secrets.token_urlsafe(16)}"
    signature = hmac.new(key, value.encode(), hashlib.sha256).hexdigest()
    return f"{value}.{signature}"


def _verify_state(platform: str, state: str) -> None:
    parts = state.rsplit(".", 1)
    if len(parts) != 2 or not parts[0].startswith(f"{platform}."):
        raise HTTPException(400, "invalid OAuth state")
    key = get_settings().token_encryption_key.encode() or b"development-only-state-key"
    expected = hmac.new(key, parts[0].encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(parts[1], expected):
        raise HTTPException(400, "invalid OAuth state signature")
    timestamp = int(parts[0].split(".")[1])
    if abs(time.time() - timestamp) > 600:
        raise HTTPException(400, "expired OAuth state")


@router.get("/oauth/{platform}/authorize")
def authorize(platform: str) -> RedirectResponse:
    try:
        url = get_publisher(platform).authorize(_state(platform))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return RedirectResponse(url)


def _oauth_html(platform: str, ok: bool, message: str) -> HTMLResponse:
    payload = json.dumps(
        {"type": "shorts-studio-oauth", "ok": ok, "platform": platform, "message": message},
        ensure_ascii=False,
    ).replace("<", r"\u003c")
    title = f"{platform.title()} подключён" if ok else "Не удалось подключить аккаунт"
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><title>Shorts Studio</title>"
        f"<body style='font:16px system-ui;background:#101416;color:#fff;padding:28px'>"
        f"<h2>{html.escape(title)}</h2><p>{html.escape(message)}</p>"
        f"<script>if(window.opener){{window.opener.postMessage({payload},window.location.origin)}}"
        "setTimeout(()=>window.close(),1600)</script></body>"
    )


@router.get("/oauth/{platform}/callback", response_class=HTMLResponse)
def oauth_callback(
    platform: str, code: str | None = None, state: str | None = None,
    error: str | None = None, error_description: str | None = None,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if platform not in PLATFORMS:
        return _oauth_html(platform, False, "Платформа не поддерживается.")
    if error:
        return _oauth_html(platform, False, error_description or error)
    if not code or not state:
        return _oauth_html(platform, False, "Платформа не вернула код авторизации.")
    try:
        _verify_state(platform, state)
        publisher = get_publisher(platform)
        credentials = publisher.exchange_code(code, state)
        if credentials.get("expires_in"):
            credentials["expires_at"] = time.time() + float(credentials["expires_in"])
        account_data = publisher.validate_account(credentials)
        if not account_data.get("valid"):
            raise RuntimeError("Платформа не подтвердила доступ к этому аккаунту.")
        external_id = account_data.get("id", "")
        account = db.scalar(select(ConnectedAccount).where(
            ConnectedAccount.platform == platform,
            ConnectedAccount.external_id == external_id,
        ))
        if not account:
            account = ConnectedAccount(platform=platform, external_id=external_id)
            db.add(account)
        account.display_name = account_data.get("name", "Connected account")
        account.encrypted_credentials = encrypt_credentials(credentials)
        account.revoked = False
        db.commit()
        return _oauth_html(platform, True, f"Аккаунт {account.display_name} подключён.")
    except Exception as exc:
        return _oauth_html(platform, False, str(exc)[:500])


@router.get("/accounts", response_model=list[AccountView])
def list_accounts(db: Session = Depends(get_db)) -> list[ConnectedAccount]:
    return list(db.scalars(select(ConnectedAccount).order_by(ConnectedAccount.created_at.desc())).all())


@router.delete("/accounts/{account_id}", status_code=204)
def revoke_account(account_id: str, db: Session = Depends(get_db)) -> None:
    account = db.get(ConnectedAccount, account_id)
    if not account:
        raise _not_found("account")
    if not account.revoked:
        get_publisher(account.platform).revoke(decrypt_credentials(account.encrypted_credentials))
        account.revoked = True
        account.encrypted_credentials = encrypt_credentials({"revoked_at": datetime.now(timezone.utc).isoformat()})
        db.commit()
