from contextlib import asynccontextmanager
import asyncio
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.router import router
from app.core.config import get_settings
from app.core.logging import configure_logging, log
from app.database.session import init_db
from app.export_pipeline.pipeline import resume_interrupted_jobs
from app.scheduler.service import dispatch_due, recover_interrupted


def _startup_trace(message: str) -> None:
    path = os.environ.get("STANDALONE_STARTUP_LOG")
    if path:
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(f"{message}\n")


async def _publishing_scheduler() -> None:
    while True:
        try:
            await asyncio.to_thread(dispatch_due)
        except Exception as exc:
            log.exception("publishing_scheduler_error", error=str(exc))
        await asyncio.sleep(15)


async def _channel_watcher() -> None:
    from app.channel_watcher.service import run_channel_cycle

    settings = get_settings()
    while True:
        try:
            await asyncio.to_thread(run_channel_cycle)
        except Exception as exc:
            log.exception("channel_watcher_error", error=str(exc))
        await asyncio.sleep(max(300, settings.channel_watch_interval_seconds))


@asynccontextmanager
async def lifespan(_: FastAPI):
    _startup_trace("lifespan entered")
    configure_logging()
    _startup_trace("logging configured")
    init_db()
    _startup_trace("database initialized")
    resumed = resume_interrupted_jobs() if get_settings().sync_processing else 0
    if resumed:
        log.warning("resumed_interrupted_video_jobs", count=resumed)
    recovered = recover_interrupted()
    if recovered:
        log.warning("requeued_interrupted_publications", count=recovered)
    scheduler_task = asyncio.create_task(_publishing_scheduler(), name="publishing-scheduler")
    watcher_task = (
        asyncio.create_task(_channel_watcher(), name="shorts-studio-channel-watcher")
        if get_settings().channel_watch_enabled else None
    )
    try:
        yield
    finally:
        tasks = [scheduler_task, *([watcher_task] if watcher_task else [])]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass


settings = get_settings()
app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",")],
    allow_methods=["*"], allow_headers=["*"], allow_credentials=True,
)
app.include_router(router)


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled_request_error", path=request.url.path, method=request.method)
    return JSONResponse(status_code=500, content={"detail": "internal server error"})
