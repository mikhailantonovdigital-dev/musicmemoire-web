from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import utcnow
from app.models import Order, OrderEvent, SongGeneration
from app.services.email_service import EmailServiceError, send_song_ready_email
from app.services.suno_service import (
    SongCallbackResult,
    SongSyncResult,
    SunoServiceError,
    parse_song_callback,
    start_song_generation,
    sync_song_generation,
)

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


def has_successful_payment(order: Order) -> bool:
    return any(payment.status == "succeeded" for payment in order.payments)


def can_start_song(order: Order) -> bool:
    return has_successful_payment(order)


def create_song_job(db: Session, order: Order, *, event_type: str = "song_generation_started") -> SongGeneration:
    latest_song = get_latest_song(order)
    if latest_song and latest_song.status in RUNNING_SONG_STATUSES:
        return latest_song

    lyrics_text = (order.final_lyrics_text or "").strip()
    if not lyrics_text:
        raise SunoServiceError("Сначала нужен финальный текст песни.")

    if order.user_id is None:
        raise SunoServiceError("Сначала нужно привязать заказ к email и кабинету.")

    if not can_start_song(order):
        raise SunoServiceError("Генерация песни станет доступна после оплаты.")

    attempt_no = (latest_song.attempt_no + 1) if latest_song else 1

    song = SongGeneration(
        order_id=order.id,
        user_id=order.user_id,
        provider="suno",
        status="queued",
        attempt_no=attempt_no,
        lyrics_text_snapshot=lyrics_text,
    )
    db.add(song)
    db.flush()

    result = start_song_generation(
        order_number=order.order_number,
        lyrics_text=lyrics_text,
        song_style=order.song_style,
        song_style_custom=order.song_style_custom,
        singer_gender=order.singer_gender,
    )

    song.external_job_id = result.external_job_id
    song.status = result.status
    song.started_at = utcnow()
    song.raw_payload = result.raw
    song.error_message = None
    order.status = "song_pending"

    db.add(
        OrderEvent(
            order=order,
            event_type=event_type,
            payload={
                "song_job_id": song.public_id,
                "attempt_no": song.attempt_no,
                "provider": song.provider,
                "external_job_id": song.external_job_id,
            },
        )
    )
    return song


def _merge_raw_payload(song: SongGeneration, sync_raw: dict[str, Any], *, bucket_name: str) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if isinstance(song.raw_payload, dict):
        payload.update(song.raw_payload)
    payload[bucket_name] = sync_raw
    return payload


def apply_song_sync_result(
    db: Session,
    song: SongGeneration,
    result: SongSyncResult,
    *,
    bucket_name: str,
    status_event_type: str,
    status_event_payload: dict[str, Any] | None = None,
) -> SongGeneration:
    previous_status = song.status
    previous_audio_url = song.audio_url

    song.status = result.status
    song.error_message = result.error_message
    song.raw_payload = _merge_raw_payload(song, result.raw, bucket_name=bucket_name)

    if result.result_tracks:
        song.result_tracks = result.result_tracks

    if result.audio_url:
        song.audio_url = result.audio_url

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

    if previous_status != song.status or previous_audio_url != song.audio_url:
        payload = {
            "song_job_id": song.public_id,
            "status_from": previous_status,
            "status_to": song.status,
            "audio_url_changed": previous_audio_url != song.audio_url,
        }
        if status_event_payload:
            payload.update(status_event_payload)
        db.add(
            OrderEvent(
                order=song.order,
                event_type=status_event_type,
                payload=payload,
            )
        )

    return song


def sync_song_job_state(db: Session, song: SongGeneration, *, event_type: str = "song_generation_status_changed") -> SongGeneration:
    if song.status not in RUNNING_SONG_STATUSES:
        return song

    result = sync_song_generation(
        external_job_id=song.external_job_id,
        started_at=song.started_at,
    )
    return apply_song_sync_result(
        db,
        song,
        result,
        bucket_name="sync",
        status_event_type=event_type,
        status_event_payload={"source": "poll"},
    )


def process_song_callback(db: Session, payload: dict[str, Any]) -> SongGeneration | None:
    callback_result: SongCallbackResult = parse_song_callback(payload)
    if not callback_result.external_job_id:
        return None

    song = (
        db.query(SongGeneration)
        .filter(SongGeneration.external_job_id == callback_result.external_job_id)
        .order_by(SongGeneration.id.desc())
        .first()
    )
    if song is None:
        return None

    db.add(
        OrderEvent(
            order=song.order,
            event_type="song_generation_callback_received",
            payload={
                "song_job_id": song.public_id,
                "external_job_id": callback_result.external_job_id,
                "callback_type": callback_result.callback_type,
                "status": callback_result.sync_result.status,
            },
        )
    )

    return apply_song_sync_result(
        db,
        song,
        callback_result.sync_result,
        bucket_name="callback",
        status_event_type="song_generation_status_changed",
        status_event_payload={
            "source": "callback",
            "callback_type": callback_result.callback_type,
        },
    )
