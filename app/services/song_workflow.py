from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import utcnow
from app.core.storage import StorageError, cache_remote_song_file, resolve_storage_path
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


def _get_primary_song_audio_url(song: SongGeneration) -> str | None:
    if song.audio_variants:
        for track in song.audio_variants:
            url = (track.get("audio_url") or track.get("stream_audio_url") or "").strip()
            if url:
                return url
    return (song.audio_url or "").strip() or None


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
            audio_url=_get_primary_song_audio_url(song),
        )
        db.add(
            OrderEvent(
                order=order,
                event_type="song_ready_email_sent",
                payload={
                    "song_job_id": song.public_id,
                    "email": order.user.email,
                    "audio_url": _get_primary_song_audio_url(song),
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


def resend_song_ready_email(db: Session, song: SongGeneration) -> None:
    order = song.order
    if order.user is None or not order.user.email:
        raise EmailServiceError("У заказа нет email для отправки письма.")

    order_url = f"{settings.BASE_URL.rstrip('/')}/account/orders/{order.public_id}"

    try:
        send_song_ready_email(
            recipient_email=order.user.email,
            order_number=order.order_number,
            order_url=order_url,
            audio_url=_get_primary_song_audio_url(song),
        )
        db.add(
            OrderEvent(
                order=order,
                event_type="song_ready_email_resent",
                payload={
                    "song_job_id": song.public_id,
                    "email": order.user.email,
                    "audio_url": _get_primary_song_audio_url(song),
                    "trigger": "admin_manual_resend",
                },
            )
        )
    except EmailServiceError as exc:
        db.add(
            OrderEvent(
                order=order,
                event_type="song_ready_email_resend_failed",
                payload={
                    "song_job_id": song.public_id,
                    "email": order.user.email,
                    "audio_url": _get_primary_song_audio_url(song),
                    "trigger": "admin_manual_resend",
                    "error": str(exc),
                },
            )
        )
        raise


def get_song_attempts(order: Order) -> list[SongGeneration]:
    if not order.song_generations:
        return []
    return sorted(order.song_generations, key=lambda item: item.id or 0, reverse=True)


def get_latest_song(order: Order) -> SongGeneration | None:
    attempts = get_song_attempts(order)
    return attempts[0] if attempts else None


def song_has_audio(song: SongGeneration | None) -> bool:
    if song is None:
        return False
    return bool(song.audio_url or song.audio_variants)


def get_latest_ready_song(order: Order) -> SongGeneration | None:
    for song in get_song_attempts(order):
        if song.status == "succeeded" and song_has_audio(song):
            return song
    return None


def has_successful_payment(order: Order) -> bool:
    return any(payment.status == "succeeded" for payment in order.payments)


def can_start_song(order: Order) -> bool:
    return has_successful_payment(order)


def get_song_track_entries(song: SongGeneration) -> list[dict[str, Any]]:
    if song.audio_variants:
        return [dict(item) for item in song.audio_variants if isinstance(item, dict)]
    if song.audio_url:
        return [{
            "index": 0,
            "title": "Вариант 1",
            "audio_url": song.audio_url,
            "stream_audio_url": song.audio_url,
        }]
    return []


def get_song_track_entry(song: SongGeneration, track_index: int) -> dict[str, Any] | None:
    tracks = get_song_track_entries(song)
    if 0 <= track_index < len(tracks):
        item = dict(tracks[track_index])
        item.setdefault("index", track_index)
        return item
    return None


def get_song_track_storage_path(song: SongGeneration, track_index: int) -> Path | None:
    track = get_song_track_entry(song, track_index)
    if track is None:
        return None
    relative_path = (track.get("stored_relative_path") or "").strip()
    if not relative_path:
        return None
    try:
        path = resolve_storage_path(relative_path)
    except StorageError:
        return None
    if not path.exists():
        return None
    return path


def ensure_song_track_cached(db: Session, song: SongGeneration, track_index: int, *, source: str) -> dict[str, Any] | None:
    track = get_song_track_entry(song, track_index)
    if track is None:
        return None

    existing_path = get_song_track_storage_path(song, track_index)
    if existing_path is not None:
        return track

    source_url = (track.get("audio_url") or track.get("stream_audio_url") or "").strip()
    if not source_url.startswith(("http://", "https://")):
        return track

    stored = cache_remote_song_file(
        source_url,
        order_number=song.order.order_number,
        song_public_id=song.public_id,
        track_index=track_index,
    )

    tracks = get_song_track_entries(song)
    if not tracks:
        return None

    updated_track = dict(tracks[track_index])
    updated_track["index"] = track_index
    updated_track["stored_relative_path"] = stored.relative_path
    updated_track["stored_content_type"] = stored.content_type
    updated_track["stored_size_bytes"] = stored.size_bytes
    updated_track["stored_original_filename"] = stored.original_filename
    updated_track["stored_at"] = utcnow().isoformat()
    tracks[track_index] = updated_track
    song.result_tracks = tracks

    if not song.audio_url:
        song.audio_url = source_url

    db.add(
        OrderEvent(
            order=song.order,
            event_type="song_asset_cached",
            payload={
                "song_job_id": song.public_id,
                "attempt_no": song.attempt_no,
                "track_index": track_index,
                "source": source,
                "stored_relative_path": stored.relative_path,
                "stored_size_bytes": stored.size_bytes,
            },
        )
    )
    return updated_track


def cache_song_assets(db: Session, song: SongGeneration, *, source: str) -> None:
    tracks = get_song_track_entries(song)
    if not tracks:
        return

    errors: list[dict[str, Any]] = []
    for track_index in range(len(tracks)):
        try:
            ensure_song_track_cached(db, song, track_index, source=source)
        except StorageError as exc:
            errors.append({
                "track_index": track_index,
                "error": str(exc),
            })

    if errors:
        db.add(
            OrderEvent(
                order=song.order,
                event_type="song_asset_cache_failed",
                payload={
                    "song_job_id": song.public_id,
                    "attempt_no": song.attempt_no,
                    "source": source,
                    "errors": errors,
                },
            )
        )


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
        cache_song_assets(db, song, source=bucket_name)
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
