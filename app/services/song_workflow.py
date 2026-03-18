from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import utcnow
from app.models import Order, OrderEvent, SongGeneration
from app.services.email_service import EmailServiceError, send_song_ready_email
from app.services.suno_service import SunoServiceError, sync_song_generation

RUNNING_SONG_STATUSES = {"queued", "processing"}


def humanize_song_status(status: str | None) -> str:
    mapping = {
        "queued": "В очереди",
        "processing": "Генерируется",
        "succeeded": "Готово",
        "failed": "Ошибка",
        "canceled": "Отменено",
    }
    return mapping.get(status or "", "—")


def has_order_event(db: Session, order: Order, event_type: str) -> bool:
    return (
        db.query(OrderEvent.id)
        .filter(OrderEvent.order_id == order.id, OrderEvent.event_type == event_type)
        .first()
        is not None
    )


def maybe_send_song_ready_email(db: Session, song: SongGeneration) -> None:
    order = song.order
    if order.user is None or not order.user.email:
        return

    if has_order_event(db, order, "song_ready_email_sent"):
        return

    order_url = f"{settings.BASE_URL.rstrip('/')}/account/orders/{order.public_id}"

    try:
        send_song_ready_email(
            recipient_email=order.user.email,
            order_number=order.order_number,
            order_url=order_url,
            audio_url=song.audio_url,
        )
        db.add(
            OrderEvent(
                order=order,
                event_type="song_ready_email_sent",
                payload={
                    "song_job_id": song.public_id,
                    "email": order.user.email,
                    "audio_url": song.audio_url,
                },
            )
        )
    except EmailServiceError as exc:
        db.add(
            OrderEvent(
                order=order,
                event_type="song_ready_email_failed",
                payload={
                    "song_job_id": song.public_id,
                    "email": order.user.email,
                    "error": str(exc),
                },
            )
        )


def get_latest_song(order: Order) -> SongGeneration | None:
    if not order.song_generations:
        return None
    return sorted(order.song_generations, key=lambda item: item.id or 0, reverse=True)[0]


def sync_song_job_state(db: Session, song: SongGeneration) -> SongGeneration:
    if song.status not in RUNNING_SONG_STATUSES:
        return song

    result = sync_song_generation(
        external_job_id=song.external_job_id,
        started_at=song.started_at,
    )

    previous_status = song.status
    song.status = result.status
    song.audio_url = result.audio_url
    song.error_message = result.error_message
    song.raw_payload = result.raw

    if result.status == "succeeded":
        if song.finished_at is None:
            song.finished_at = utcnow()
        song.order.status = "song_ready"
        maybe_send_song_ready_email(db, song)
    elif result.status == "failed":
        if song.finished_at is None:
            song.finished_at = utcnow()
        song.order.status = "song_failed"
    else:
        song.order.status = "song_pending"

    if previous_status != song.status:
        db.add(
            OrderEvent(
                order=song.order,
                event_type="song_generation_status_changed",
                payload={
                    "song_job_id": song.public_id,
                    "status_from": previous_status,
                    "status_to": song.status,
                },
            )
        )

    return song


def mark_song_sync_failed(
    db: Session,
    song: SongGeneration,
    *,
    error_text: str,
    event_type: str = "song_generation_failed",
) -> SongGeneration:
    song.status = "failed"
    song.error_message = error_text
    song.finished_at = utcnow()
    song.order.status = "song_failed"
    db.add(
        OrderEvent(
            order=song.order,
            event_type=event_type,
            payload={
                "song_job_id": song.public_id,
                "error": error_text,
            },
        )
    )
    return song
