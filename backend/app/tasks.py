from celery import Celery

from app.core.config import get_settings
from app.export_pipeline.pipeline import process_job


settings = get_settings()
celery_app = Celery("shorts_studio", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    timezone="UTC",
)


@celery_app.task(bind=True, autoretry_for=(OSError,), retry_backoff=True, retry_jitter=True, max_retries=5)
def process_video_task(self, job_id: str) -> None:
    process_job(job_id)


@celery_app.task
def dispatch_publications_task() -> int:
    from app.scheduler.service import dispatch_due

    return dispatch_due()


celery_app.conf.beat_schedule = {
    "dispatch-due-publications": {"task": "app.tasks.dispatch_publications_task", "schedule": 30.0}
}

