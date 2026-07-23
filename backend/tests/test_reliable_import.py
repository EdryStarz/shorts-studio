import asyncio
from types import SimpleNamespace

from app.api import router as api_router
from app.api.schemas import YouTubeImportRequest
from app.database.models import Job, Source
from app.database.session import SessionLocal
from app.ingestion.youtube_links import DownloadedVideo


def test_youtube_import_atomically_starts_and_restores_job(tmp_path, monkeypatch):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    downloaded = DownloadedVideo(
        path=video,
        original_name="video.mp4",
        checksum="a" * 64,
        size_bytes=video.stat().st_size,
    )
    monkeypatch.setattr(api_router, "download_youtube_video", lambda *_: downloaded)
    monkeypatch.setattr(api_router, "get_settings", lambda: SimpleNamespace(sync_processing=False))
    dispatched = []
    monkeypatch.setattr(api_router.process_video_task, "delay", dispatched.append)
    payload = YouTubeImportRequest(
        url="https://www.youtube.com/watch?v=gwFG4nErAPE",
        browser="edge",
        rights_confirmed=True,
    )

    with SessionLocal() as db:
        first = asyncio.run(api_router.import_youtube_and_process(payload, db))
        second = asyncio.run(api_router.import_youtube_and_process(payload, db))
        latest = api_router.latest_source_job(first.source.id, db)

        assert first.source.id == second.source.id
        assert first.job.id == second.job.id
        assert latest is not None and latest.id == first.job.id
        assert db.query(Source).count() == 1
        assert db.query(Job).count() == 1
        assert dispatched == [first.job.id]
