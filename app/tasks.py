from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models import BackgroundJob, LyricsVersion, Order, OrderEvent, OrderPayment, SongGeneration, VoiceInput
from app.core.storage import StorageError, ensure_voice_input_local_path
from app.services.background_jobs import mark_job_failed, mark_job_started, mark_job_succeeded
from app.services.email_service import EmailServiceError, send_payment_success_email, send_song_ready_email
from app.services.lyrics_generation_service import DualGenerationResult, LyricsGenerationError, generate_dual_lyrics_versions
from app.services.song_workflow import start_song_job_now
from app.services.transcription_service import TranscriptionServiceError, transcribe_audio_file



def _get_background_job(db: Session, background_job_public_id: str) -> BackgroundJob:
    background_job = (
        db.query(BackgroundJob)
        .filter(BackgroundJob.public_id == background_job_public_id)
        .first()
    )
    if background_job is None:
        raise RuntimeError("Background job not found.")
    return background_job



def _get_order(db: Session, order_public_id: str) -> Order:
    order = db.query(Order).filter(Order.public_id == order_public_id).first()
    if order is None:
        raise RuntimeError("Order not found.")
    return order



def _get_voice_input(db: Session, voice_public_id: str) -> VoiceInput:
    voice = db.query(VoiceInput).filter(VoiceInput.public_id == voice_public_id).first()
    if voice is None:
        raise RuntimeError("Voice input not found.")
    return voice



def _get_song(db: Session, song_public_id: str) -> SongGeneration:
    song = db.query(SongGeneration).filter(SongGeneration.public_id == song_public_id).first()
    if song is None:
        raise RuntimeError("Song generation not found.")
    return song



def _get_payment(db: Session, payment_public_id: str) -> OrderPayment:
    payment = db.query(OrderPayment).filter(OrderPayment.public_id == payment_public_id).first()
    if payment is None:
        raise RuntimeError("Payment not found.")
    return payment



def run_voice_transcription_task(
    *,
    background_job_public_id: str,
    order_public_id: str,
    voice_public_id: str,
    apply_to_order: bool,
    started_event_type: str,
    success_event_type: str,
    failure_event_type: str,
    trigger: str,
) -> None:
    db = SessionLocal()
    try:
        background_job = _get_background_job(db, background_job_public_id)
        order = _get_order(db, order_public_id)
        voice_input = _get_voice_input(db, voice_public_id)

        mark_job_started(db, background_job)
        voice_input.transcription_status = "transcribing"
        db.add(
            OrderEvent(
                order=order,
                event_type=started_event_type,
                payload={
                    "voice_input_id": voice_input.public_id,
                    "background_job_id": background_job.public_id,
                    "trigger": trigger,
                },
            )
        )
        db.commit()

        try:
            local_path = ensure_voice_input_local_path(voice_input)
            result = asyncio.run(transcribe_audio_file(str(local_path)))
        except (TranscriptionServiceError, StorageError) as exc:
            db.rollback()
            voice_input = _get_voice_input(db, voice_public_id)
            order = _get_order(db, order_public_id)
            background_job = _get_background_job(db, background_job_public_id)
            voice_input.transcription_status = "failed"
            db.add(
                OrderEvent(
                    order=order,
                    event_type=failure_event_type,
                    payload={
                        "voice_input_id": voice_input.public_id,
                        "background_job_id": background_job.public_id,
                        "error": str(exc),
                        "trigger": trigger,
                    },
                )
            )
            mark_job_failed(db, background_job, error_message=str(exc))
            db.commit()
            return

        voice_input = _get_voice_input(db, voice_public_id)
        order = _get_order(db, order_public_id)
        background_job = _get_background_job(db, background_job_public_id)
        voice_input.transcription_status = "done"
        voice_input.transcript_text = result.text
        if apply_to_order:
            order.transcript_text = result.text
        db.add(
            OrderEvent(
                order=order,
                event_type=success_event_type,
                payload={
                    "voice_input_id": voice_input.public_id,
                    "background_job_id": background_job.public_id,
                    "text_length": len(result.text),
                    "applied_to_order": apply_to_order,
                    "trigger": trigger,
                },
            )
        )
        mark_job_succeeded(
            db,
            background_job,
            result_payload={
                "voice_input_id": voice_input.public_id,
                "text_length": len(result.text),
                "applied_to_order": apply_to_order,
            },
        )
        db.commit()
    finally:
        db.close()



def run_admin_lyrics_regeneration_task(*, background_job_public_id: str, order_public_id: str) -> None:
    db = SessionLocal()
    try:
        background_job = _get_background_job(db, background_job_public_id)
        order = _get_order(db, order_public_id)
        source_text = (order.transcript_text if order.story_source == "voice" else order.story_text or "").strip()

        mark_job_started(db, background_job)
        db.add(
            OrderEvent(
                order=order,
                event_type="admin_lyrics_regeneration_started",
                payload={
                    "story_source": order.story_source,
                    "text_length": len(source_text),
                    "trigger": "admin_background_regenerate",
                    "background_job_id": background_job.public_id,
                },
            )
        )
        db.commit()

        try:
            result: DualGenerationResult = asyncio.run(generate_dual_lyrics_versions(source_text))
        except LyricsGenerationError as exc:
            db.rollback()
            background_job = _get_background_job(db, background_job_public_id)
            order = _get_order(db, order_public_id)
            db.add(
                OrderEvent(
                    order=order,
                    event_type="admin_lyrics_regeneration_failed",
                    payload={
                        "error": str(exc),
                        "trigger": "admin_background_regenerate",
                        "background_job_id": background_job.public_id,
                    },
                )
            )
            mark_job_failed(db, background_job, error_message=str(exc))
            db.commit()
            return

        order = _get_order(db, order_public_id)
        background_job = _get_background_job(db, background_job_public_id)
        db.query(LyricsVersion).filter(LyricsVersion.order_id == order.id).delete(synchronize_session=False)

        selected_version_id = None
        final_text = None
        for index, item in enumerate(result.versions):
            version = LyricsVersion(
                order_id=order.id,
                provider=item.provider,
                model_name=item.model_name,
                angle_label=item.angle_label,
                prompt_text=item.prompt_text,
                lyrics_text=item.lyrics_text,
                edited_lyrics_text=None,
                is_selected=index == 0,
            )
            db.add(version)
            db.flush()
            if index == 0:
                selected_version_id = version.public_id
                final_text = item.lyrics_text

        if final_text:
            order.final_lyrics_text = final_text

        variant_errors = [
            {
                "slot_label": err.slot_label,
                "user_message": err.user_message,
                "technical_message": err.technical_message,
            }
            for err in result.errors
        ]

        db.add(
            OrderEvent(
                order=order,
                event_type="admin_lyrics_regeneration_done",
                payload={
                    "versions_count": len(result.versions),
                    "selected_version_id": selected_version_id,
                    "model": result.versions[0].model_name if result.versions else None,
                    "errors": variant_errors,
                    "trigger": "admin_background_regenerate",
                    "background_job_id": background_job.public_id,
                },
            )
        )
        mark_job_succeeded(
            db,
            background_job,
            result_payload={
                "versions_count": len(result.versions),
                "selected_version_id": selected_version_id,
            },
        )
        db.commit()
    finally:
        db.close()



def run_song_start_task(
    *,
    background_job_public_id: str,
    song_public_id: str,
    started_event_type: str,
    failed_event_type: str,
    trigger: str,
    payment_public_id: str | None = None,
) -> None:
    db = SessionLocal()
    try:
        background_job = _get_background_job(db, background_job_public_id)
        song = _get_song(db, song_public_id)
        order = song.order
        payment = _get_payment(db, payment_public_id) if payment_public_id else None

        mark_job_started(db, background_job)
        db.commit()

        try:
            start_song_job_now(
                db,
                song,
                started_event_type=started_event_type,
                failed_event_type=failed_event_type,
                trigger=trigger,
                payment=payment,
                background_job=background_job,
            )
            if song.status == "failed":
                mark_job_failed(
                    db,
                    background_job,
                    error_message=song.error_message or "Не удалось запустить генерацию песни.",
                    result_payload={
                        "song_job_id": song.public_id,
                        "external_job_id": song.external_job_id,
                        "status": song.status,
                    },
                )
            else:
                mark_job_succeeded(
                    db,
                    background_job,
                    result_payload={
                        "song_job_id": song.public_id,
                        "external_job_id": song.external_job_id,
                        "status": song.status,
                    },
                )
            db.commit()
        except Exception as exc:
            db.rollback()
            background_job = _get_background_job(db, background_job_public_id)
            mark_job_failed(db, background_job, error_message=str(exc))
            db.commit()
    finally:
        db.close()



def run_payment_success_email_task(*, background_job_public_id: str, order_public_id: str, payment_public_id: str) -> None:
    db = SessionLocal()
    try:
        background_job = _get_background_job(db, background_job_public_id)
        order = _get_order(db, order_public_id)
        payment = _get_payment(db, payment_public_id)
        mark_job_started(db, background_job)
        db.commit()

        if order.user is None or not order.user.email:
            mark_job_failed(db, background_job, error_message="У заказа нет email для отправки письма.")
            db.commit()
            return

        from app.core.config import settings

        order_url = f"{settings.BASE_URL.rstrip('/')}/account/orders/{order.public_id}"
        try:
            send_payment_success_email(
                recipient_email=order.user.email,
                order_number=order.order_number,
                order_url=order_url,
                price_rub=payment.final_amount_rub,
            )
        except EmailServiceError as exc:
            db.rollback()
            background_job = _get_background_job(db, background_job_public_id)
            order = _get_order(db, order_public_id)
            db.add(
                OrderEvent(
                    order=order,
                    event_type="payment_success_email_failed",
                    payload={
                        "payment_public_id": payment.public_id,
                        "email": order.user.email,
                        "error": str(exc),
                        "background_job_id": background_job.public_id,
                    },
                )
            )
            mark_job_failed(db, background_job, error_message=str(exc))
            db.commit()
            return

        order = _get_order(db, order_public_id)
        background_job = _get_background_job(db, background_job_public_id)
        db.add(
            OrderEvent(
                order=order,
                event_type="payment_success_email_sent",
                payload={
                    "payment_public_id": payment.public_id,
                    "email": order.user.email,
                    "background_job_id": background_job.public_id,
                },
            )
        )
        mark_job_succeeded(db, background_job, result_payload={"email": order.user.email})
        db.commit()
    finally:
        db.close()



def run_song_ready_email_task(*, background_job_public_id: str, order_public_id: str, song_public_id: str, audio_url: str | None = None) -> None:
    db = SessionLocal()
    try:
        background_job = _get_background_job(db, background_job_public_id)
        order = _get_order(db, order_public_id)
        song = _get_song(db, song_public_id)
        mark_job_started(db, background_job)
        db.commit()

        if order.user is None or not order.user.email:
            mark_job_failed(db, background_job, error_message="У заказа нет email для отправки письма.")
            db.commit()
            return

        from app.core.config import settings

        order_url = f"{settings.BASE_URL.rstrip('/')}/account/orders/{order.public_id}"
        safe_audio_url = audio_url or song.audio_url
        try:
            send_song_ready_email(
                recipient_email=order.user.email,
                order_number=order.order_number,
                order_url=order_url,
                audio_url=safe_audio_url,
            )
        except EmailServiceError as exc:
            db.rollback()
            background_job = _get_background_job(db, background_job_public_id)
            order = _get_order(db, order_public_id)
            db.add(
                OrderEvent(
                    order=order,
                    event_type="song_ready_email_failed",
                    payload={
                        "song_job_id": song.public_id,
                        "email": order.user.email,
                        "error": str(exc),
                        "background_job_id": background_job.public_id,
                    },
                )
            )
            mark_job_failed(db, background_job, error_message=str(exc))
            db.commit()
            return

        order = _get_order(db, order_public_id)
        background_job = _get_background_job(db, background_job_public_id)
        db.add(
            OrderEvent(
                order=order,
                event_type="song_ready_email_sent",
                payload={
                    "song_job_id": song.public_id,
                    "email": order.user.email,
                    "audio_url": safe_audio_url,
                    "background_job_id": background_job.public_id,
                },
            )
        )
        mark_job_succeeded(db, background_job, result_payload={"email": order.user.email, "song_job_id": song.public_id})
        db.commit()
    finally:
        db.close()
