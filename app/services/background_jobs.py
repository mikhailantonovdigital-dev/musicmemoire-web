from __future__ import annotations

from collections.abc import Callable
from typing import Any

from redis import Redis
from rq import Queue
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import utcnow
from app.models import BackgroundJob, Order, OrderEvent


class BackgroundJobError(RuntimeError):
    pass


DEFAULT_QUEUE_NAME = "default"
JOB_TIMEOUTS: dict[str, int] = {
    "voice_transcription": 5 * 60,
    "lyrics_regeneration": 6 * 60,
    "song_generation_start": 4 * 60,
    "payment_success_email": 60,
    "song_ready_email": 60,
}
JOB_LABELS: dict[str, str] = {
    "voice_transcription": "Расшифровка голосового",
    "lyrics_regeneration": "Перегенерация текстов",
    "song_generation_start": "Запуск генерации песни",
    "payment_success_email": "Письмо об успешной оплате",
    "song_ready_email": "Письмо о готовой песне",
}


def get_job_label(job_type: str | None) -> str:
    return JOB_LABELS.get(job_type or "", job_type or "—")


_redis_connection: Redis | None = None


def get_redis_connection() -> Redis:
    global _redis_connection
    if _redis_connection is None:
        _redis_connection = Redis.from_url(settings.REDIS_URL)
    return _redis_connection



def get_queue(queue_name: str = DEFAULT_QUEUE_NAME) -> Queue:
    return Queue(name=queue_name, connection=get_redis_connection())



def find_active_job_for_order(db: Session, order: Order, job_type: str) -> BackgroundJob | None:
    return (
        db.query(BackgroundJob)
        .filter(
            BackgroundJob.order_id == order.id,
            BackgroundJob.job_type == job_type,
            BackgroundJob.status.in_(["queued", "started"]),
        )
        .order_by(BackgroundJob.id.desc())
        .first()
    )



def mark_job_started(db: Session, background_job: BackgroundJob, *, extra: dict[str, Any] | None = None) -> None:
    background_job.status = "started"
    background_job.started_at = utcnow()
    background_job.error_message = None
    if extra:
        current = background_job.result_payload if isinstance(background_job.result_payload, dict) else {}
        current.update(extra)
        background_job.result_payload = current
    if background_job.order is not None:
        db.add(
            OrderEvent(
                order=background_job.order,
                event_type="background_job_started",
                payload={
                    "background_job_id": background_job.public_id,
                    "job_type": background_job.job_type,
                    "queue_name": background_job.queue_name,
                },
            )
        )



def mark_job_succeeded(db: Session, background_job: BackgroundJob, *, result_payload: dict[str, Any] | None = None) -> None:
    background_job.status = "succeeded"
    background_job.finished_at = utcnow()
    background_job.error_message = None
    if result_payload is not None:
        background_job.result_payload = result_payload
    if background_job.order is not None:
        db.add(
            OrderEvent(
                order=background_job.order,
                event_type="background_job_succeeded",
                payload={
                    "background_job_id": background_job.public_id,
                    "job_type": background_job.job_type,
                    "queue_name": background_job.queue_name,
                },
            )
        )



def mark_job_failed(db: Session, background_job: BackgroundJob, *, error_message: str, result_payload: dict[str, Any] | None = None) -> None:
    background_job.status = "failed"
    background_job.finished_at = utcnow()
    background_job.error_message = error_message
    if result_payload is not None:
        background_job.result_payload = result_payload
    if background_job.order is not None:
        db.add(
            OrderEvent(
                order=background_job.order,
                event_type="background_job_failed",
                payload={
                    "background_job_id": background_job.public_id,
                    "job_type": background_job.job_type,
                    "queue_name": background_job.queue_name,
                    "error": error_message,
                },
            )
        )



def enqueue_background_job(
    db: Session,
    *,
    order: Order | None,
    job_type: str,
    func: Callable[..., Any],
    payload: dict[str, Any] | None = None,
    queue_name: str = DEFAULT_QUEUE_NAME,
    force_sync: bool = False,
) -> BackgroundJob:
    background_job = BackgroundJob(
        order_id=order.id if order is not None else None,
        job_type=job_type,
        queue_name=queue_name,
        status="queued",
        payload=payload or {},
    )
    db.add(background_job)
    db.flush()

    task_kwargs = dict(payload or {})
    task_kwargs["background_job_public_id"] = background_job.public_id

    if order is not None:
        db.add(
            OrderEvent(
                order=order,
                event_type="background_job_queued",
                payload={
                    "background_job_id": background_job.public_id,
                    "job_type": job_type,
                    "queue_name": queue_name,
                },
            )
        )

    if force_sync or settings.BACKGROUND_JOBS_SYNC_MODE:
        background_job.rq_job_id = f"sync-{background_job.public_id}"
        db.commit()
        func(**task_kwargs)
        db.refresh(background_job)
        return background_job

    try:
        queue = get_queue(queue_name)
        rq_job = queue.enqueue(
            func,
            kwargs=task_kwargs,
            job_timeout=JOB_TIMEOUTS.get(job_type, 5 * 60),
        )
    except Exception as exc:
        background_job.status = "failed"
        background_job.finished_at = utcnow()
        background_job.error_message = str(exc)
        if order is not None:
            db.add(
                OrderEvent(
                    order=order,
                    event_type="background_job_enqueue_failed",
                    payload={
                        "background_job_id": background_job.public_id,
                        "job_type": job_type,
                        "queue_name": queue_name,
                        "error": str(exc),
                    },
                )
            )
        raise BackgroundJobError(str(exc)) from exc

    background_job.rq_job_id = rq_job.id
    return background_job
