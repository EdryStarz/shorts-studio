from pathlib import Path
import threading

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.clip_scoring.service import assess_source_virality, build_candidates, source_clip_budget
from app.core.config import get_settings
from app.core.logging import log
from app.database.models import (
    ChannelVideo, Clip, Job, JobStatus, Source, StageRun, StageStatus, TranscriptSegment, utcnow,
)
from app.database.session import SessionLocal
from app.ingestion.service import checksum_file
from app.media_probe.service import extract_audio, probe_media
from app.multimodal_analysis.service import analyze, deserialize, serialize as serialize_signals
from app.profanity_filter.service import find_all_matches, mask_transcript, mask_words
from app.publishing_metadata import build_publication_metadata
from app.scene_detection.service import detect_scenes
from app.semantic_ranking.service import score_highlight_texts
from app.subtitle_renderer.service import write_ass, write_srt, write_vtt
from app.transcription.service import Segment, Word, serialize, transcribe

from .service import render_vertical_clip


STAGES = ("probe", "transcribe", "filter", "scenes", "signals", "score", "render")


def _stage(db: Session, job: Job, name: str) -> StageRun:
    stage = db.scalar(select(StageRun).where(StageRun.job_id == job.id, StageRun.name == name))
    if not stage:
        stage = StageRun(job_id=job.id, name=name)
        db.add(stage)
        db.commit()
        db.refresh(stage)
    return stage


def _complete(db: Session, stage: StageRun, artifact: dict) -> None:
    stage.status = StageStatus.completed
    stage.artifact = artifact
    stage.error = None
    stage.completed_at = utcnow()
    db.commit()


def _run_stage(db: Session, job: Job, name: str, callback):
    stage = _stage(db, job, name)
    if stage.status == StageStatus.completed:
        return stage.artifact or {}
    stage.status = StageStatus.running
    stage.started_at = utcnow()
    stage.error = None
    db.commit()
    try:
        result = callback()
        _complete(db, stage, result)
        return result
    except Exception as exc:
        stage.status = StageStatus.failed
        stage.error = str(exc)[:4000]
        stage.completed_at = utcnow()
        db.commit()
        raise


def _segments_from_db(db: Session, source_id: str) -> list[Segment]:
    rows = db.scalars(select(TranscriptSegment).where(TranscriptSegment.source_id == source_id).order_by(TranscriptSegment.start)).all()
    return [
        Segment(row.start, row.end, row.text, tuple(Word(**word) for word in row.words))
        for row in rows
    ]


def process_job(job_id: str) -> None:
    settings = get_settings()
    with SessionLocal() as db:
        job = db.get(Job, job_id)
        if not job:
            raise ValueError(f"job {job_id} not found")
        source = db.get(Source, job.source_id)
        if not source:
            raise ValueError(f"source {job.source_id} not found")
        job.status = JobStatus.running
        job.error = None
        source.import_status = "processing"
        source.import_error = None
        db.commit()
        path = Path(source.storage_path)
        work = settings.work_dir / source.id
        work.mkdir(parents=True, exist_ok=True)
        try:
            probe = _run_stage(db, job, "probe", lambda: _probe(db, source, path))
            job.progress = 15
            db.commit()
            _run_stage(
                db, job, "transcribe",
                lambda: _transcribe(db, source, path, work, bool(probe["has_audio"])),
            )
            job.progress = 45
            db.commit()
            source_filter = _run_stage(
                db, job, "filter",
                lambda: _filter_source(db, source, float(probe["duration"])),
            )
            if not source_filter.get("accepted", False):
                db.execute(delete(Clip).where(Clip.source_id == source.id))
                job.progress = 100
                job.status = JobStatus.completed
                source.import_status = "completed"
                db.commit()
                log.info(
                    "pipeline_source_rejected",
                    job_id=job.id, source_id=source.id,
                    virality_score=source_filter.get("score"),
                )
                return
            scenes = _run_stage(
                db, job, "scenes", lambda: _scenes(path, float(probe["duration"])),
            )
            job.progress = 55
            db.commit()
            signals = _run_stage(
                db, job, "signals",
                lambda: _signals(
                    db, source, path, work / "audio.wav", float(probe["duration"]),
                    bool(probe["has_audio"]),
                ),
            )
            job.progress = 65
            db.commit()
            _run_stage(
                db, job, "score",
                lambda: _score(
                    db, source, scenes, signals, float(probe["duration"]), source_filter,
                ),
            )
            job.progress = 75
            db.commit()
            _run_stage(db, job, "render", lambda: _render(db, source, path, probe, job))
            job.progress = 100
            job.status = JobStatus.completed
            source.import_status = "completed"
            source.import_error = None
            db.commit()
            log.info("pipeline_completed", job_id=job.id, source_id=source.id)
        except Exception as exc:
            job.status = JobStatus.failed
            job.error = str(exc)[:4000]
            source.import_status = "failed"
            source.import_error = str(exc)[:4000]
            for clip in db.scalars(select(Clip).where(Clip.source_id == source.id)).all():
                if clip.processing_status != "completed":
                    clip.processing_status = "failed"
            db.commit()
            log.exception("pipeline_failed", job_id=job.id, source_id=source.id)
            raise


def resume_interrupted_jobs() -> int:
    """Resume jobs left queued/running by an application restart, reusing completed stages."""
    with SessionLocal() as db:
        job_ids = list(db.scalars(
            select(Job.id).where(Job.status.in_((JobStatus.queued, JobStatus.running)))
        ).all())
    for job_id in job_ids:
        threading.Thread(
            target=process_job, args=(job_id,), daemon=True, name=f"resume-{job_id}",
        ).start()
    return len(job_ids)


def _probe(db: Session, source: Source, path: Path) -> dict:
    info = probe_media(path)
    source.duration, source.width, source.height = info.duration, info.width, info.height
    source.has_audio = info.has_audio
    db.commit()
    return info.__dict__


def _transcribe(db: Session, source: Source, path: Path, work: Path, has_audio: bool) -> dict:
    if not has_audio:
        db.execute(delete(TranscriptSegment).where(TranscriptSegment.source_id == source.id))
        db.commit()
        return {"segments": [], "count": 0}
    audio = extract_audio(path, work / "audio.wav")
    segments = transcribe(audio)
    db.execute(delete(TranscriptSegment).where(TranscriptSegment.source_id == source.id))
    for segment in segments:
        db.add(TranscriptSegment(
            source_id=source.id, start=segment.start, end=segment.end, text=segment.text,
            words=[word.__dict__ for word in segment.words],
        ))
    db.commit()
    return {"segments": serialize(segments), "count": len(segments)}


def _scenes(path: Path, duration: float) -> dict:
    if duration > 1800:
        # Full-frame scene scanning is redundant for hour-long streams: the
        # multimodal signal stage still measures visual progression, while
        # transcript/story boundaries determine the final cut. Avoid leaving
        # the UI apparently frozen on a multi-minute auxiliary pass.
        return {
            "scenes": [], "count": 0,
            "strategy": "transcript_and_multimodal_for_long_source",
        }
    scenes = detect_scenes(path)
    return {
        "scenes": [scene.__dict__ for scene in scenes], "count": len(scenes),
        "strategy": "sampled_content_detection",
    }


def _filter_source(db: Session, source: Source, duration: float) -> dict:
    segments = _segments_from_db(db, source.id)
    channel_video = db.scalar(select(ChannelVideo).where(ChannelVideo.source_id == source.id))
    metadata_text = source.original_name
    if channel_video:
        tags = " ".join(str(tag) for tag in channel_video.video_metadata.get("tags", ()))
        metadata_text = f"{channel_video.title} {channel_video.description} {tags}"
    assessment = assess_source_virality(metadata_text, segments, duration)
    return {
        "config_version": "shorts-studio-virality-v1",
        "accepted": assessment.accepted,
        "score": assessment.score,
        "threshold": assessment.threshold,
        "details": assessment.details,
        "semantic_core": list(assessment.semantic_core),
    }


def _signals(
    db: Session, source: Source, video_path: Path, audio_path: Path, duration: float,
    has_audio: bool,
) -> dict:
    settings = get_settings()
    if not settings.multimodal_analysis_enabled or not has_audio:
        return {"signals": [], "count": 0, "provider": "disabled"}
    segments = _segments_from_db(db, source.id)
    try:
        signals = analyze(
            video_path, audio_path, segments, duration,
            bucket_seconds=settings.signal_bucket_seconds,
        )
        return {"signals": serialize_signals(signals), "count": len(signals), "provider": "local"}
    except Exception as exc:
        log.warning("multimodal_signal_fallback", error=str(exc))
        return {"signals": [], "count": 0, "provider": "fallback"}


def _score(
    db: Session, source: Source, scene_artifact: dict, signal_artifact: dict, duration: float,
    source_filter: dict,
) -> dict:
    from app.scene_detection.service import Scene

    settings = get_settings()
    segments = _segments_from_db(db, source.id)
    scenes = [Scene(**item) for item in scene_artifact.get("scenes", [])]
    signals = deserialize(signal_artifact.get("signals", []))
    semantic_scorer = None
    if settings.semantic_ranking_enabled:
        def semantic_scorer(texts: list[str]) -> list[float]:
            try:
                return score_highlight_texts(texts)
            except Exception as exc:
                log.warning("semantic_ranking_fallback", error=str(exc))
                return []

    clip_budget = source_clip_budget(float(source_filter.get("score", 0.0)))
    candidates = build_candidates(
        segments, scenes, duration, settings.min_clip_seconds, settings.max_clip_seconds,
        limit=max(12, clip_budget * 6), semantic_scorer=semantic_scorer,
        signals=signals,
    )
    from app.local_director.service import select_and_rerank

    candidates = select_and_rerank(candidates, clip_budget)
    db.execute(delete(Clip).where(Clip.source_id == source.id))
    for sequence_number, candidate in enumerate(candidates, 1):
        matches = find_all_matches(list(candidate.words))
        subtitle_text = mask_transcript(candidate.transcript, matches)
        metadata = build_publication_metadata(
            subtitle_text, "youtube",
        )
        db.add(Clip(
            source_id=source.id, sequence_number=sequence_number,
            title=metadata["display_title"], description=metadata["description"],
            hashtags=metadata["tags"], processing_status="processing",
            start=candidate.start, end=candidate.end, score=candidate.score,
            score_details=candidate.details, transcript=candidate.transcript,
            words=list(candidate.words), subtitle_text=subtitle_text,
        ))
    db.commit()
    return {
        "count": len(candidates), "clip_budget": clip_budget,
        "virality_score": source_filter.get("score"),
    }


def _render(db: Session, source: Source, source_path: Path, probe: dict, job: Job | None = None) -> dict:
    settings = get_settings()
    clips = db.scalars(select(Clip).where(Clip.source_id == source.id).order_by(Clip.score.desc())).all()
    outputs = []
    quality_reports = []
    for index, clip in enumerate(clips):
        destination_dir = settings.export_dir / source.id / clip.id
        destination_dir.mkdir(parents=True, exist_ok=True)
        words = clip.words
        matches = find_all_matches(words)
        masked_words = mask_words(words, matches)
        ass_path = write_ass(masked_words, destination_dir / "captions.ass", clip.start, dynamic=True)
        write_srt(masked_words, destination_dir / "captions.srt", clip.start)
        write_vtt(masked_words, destination_dir / "captions.vtt", clip.start)
        quality_report: dict = {}
        output = render_vertical_clip(
            source=source_path, destination=destination_dir / "short.mp4", subtitle_path=ass_path,
            start=clip.start, end=clip.end, source_width=int(probe["width"]),
            source_height=int(probe["height"]), profanity_matches=matches, mode="split",
            has_audio=bool(probe["has_audio"]), source_fps=float(probe.get("fps") or 0),
            quality_report=quality_report,
        )
        clip.export_path = str(output.resolve())
        clip.preview_path = str(output.resolve())
        clip.subtitle_path = str(ass_path.resolve())
        clip.checksum = checksum_file(output)
        clip.processing_status = "completed"
        if job is not None:
            job.progress = min(99, 75 + round(24 * (index + 1) / max(len(clips), 1)))
        db.commit()
        outputs.append(str(output))
        quality_reports.append(quality_report)
    return {"outputs": outputs, "count": len(outputs), "quality": quality_reports}
