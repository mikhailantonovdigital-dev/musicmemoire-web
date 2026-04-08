from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session
from sqlalchemy import or_

from app.core.config import settings
from app.core.security import utcnow
from app.models import BackgroundJob, Order, OrderEvent, OrderPayment, SongGeneration
from app.services.background_jobs import BackgroundJobError, enqueue_background_job, find_active_job_for_order
from app.services.song_workflow import create_song_job_record
from app.services.yookassa_service import fetch_payment

FINAL_PAYMENT_STATUSES = {"succeeded", "canceled"}
PENDING_PAYMENT_STATUSES = {"pending", "waiting_for_capture"}
RUNNING_SONG_STATUSES = {"queued", "processing"}
TERMINAL_SONG_STATUSES = {"succeeded", "failed", "canceled"}


def get_latest_song(order: Order) -> SongGeneration | None:
    if not order.song_generations:
        return None
    return sorted(order.song_generations, key=lambda item: item.id or 0, reverse=True)[0]


def has_order_event(db: Session, order: Order, event_type: str) -> bool:
    return (
        db.query(OrderEvent.id)
        .filter(OrderEvent.order_id == order.id, OrderEvent.event_type == event_type)
        .first()
        is not None
    )


def maybe_send_payment_success_email(db: Session, order: Order, payment: OrderPayment) -> BackgroundJob | None:
    if order.user is None or not order.user.email:
        return None

    if has_order_event(db, order, "payment_success_email_sent"):
        return None

    active_job = find_active_job_for_order(db, order, "payment_success_email")
    if active_job is not None:
        return active_job

    from app.tasks import run_payment_success_email_task

    try:
        return enqueue_background_job(
            db,
            order=order,
            job_type="payment_success_email",
            func=run_payment_success_email_task,
            payload={
                "order_public_id": order.public_id,
                "payment_public_id": payment.public_id,
            },
        )
    except BackgroundJobError as exc:
        db.add(
            OrderEvent(
                order=order,
                event_type="payment_success_email_failed",
                payload={
                    "payment_public_id": payment.public_id,
                    "email": order.user.email,
                    "error": str(exc),
                },
            )
        )
        return None

def resend_payment_success_email(db: Session, order: Order, payment: OrderPayment) -> BackgroundJob | None:
    if order.user is None or not order.user.email:
        raise RuntimeError("У заказа нет email для отправки письма.")

    from app.tasks import run_payment_success_email_task

    try:
        background_job = enqueue_background_job(
            db,
            order=order,
            job_type="payment_success_email",
            func=run_payment_success_email_task,
            payload={
                "order_public_id": order.public_id,
                "payment_public_id": payment.public_id,
            },
        )
        db.add(
            OrderEvent(
                order=order,
                event_type="payment_success_email_resent",
                payload={
                    "payment_public_id": payment.public_id,
                    "email": order.user.email,
                    "trigger": "admin_manual_resend",
                    "background_job_id": background_job.public_id,
                },
            )
        )
        return background_job
    except BackgroundJobError as exc:
        db.add(
            OrderEvent(
                order=order,
                event_type="payment_success_email_resend_failed",
                payload={
                    "payment_public_id": payment.public_id,
                    "email": order.user.email,
                    "trigger": "admin_manual_resend",
                    "error": str(exc),
                },
            )
        )
        raise RuntimeError(str(exc)) from exc


def maybe_start_song_generation_after_payment(
    db: Session,
    order: Order,
    payment: OrderPayment,
    *,
    trigger: str,
) -> SongGeneration | None:
    latest_song = get_latest_song(order)
    if latest_song and latest_song.status in RUNNING_SONG_STATUSES | TERMINAL_SONG_STATUSES:
        return latest_song

    lyrics_text = (order.final_lyrics_text or "").strip()
    if not lyrics_text:
        db.add(
            OrderEvent(
                order=order,
                event_type="song_generation_autostart_skipped",
                payload={
                    "payment_public_id": payment.public_id,
                    "trigger": trigger,
                    "reason": "missing_final_lyrics",
                },
            )
        )
        return None

    if order.user_id is None:
        db.add(
            OrderEvent(
                order=order,
                event_type="song_generation_autostart_skipped",
                payload={
                    "payment_public_id": payment.public_id,
                    "trigger": trigger,
                    "reason": "missing_user",
                },
            )
        )
        return None

    active_job = find_active_job_for_order(db, order, "song_generation_start")
    if active_job is not None:
        existing_song = next((item for item in order.song_generations if item.status in RUNNING_SONG_STATUSES), None)
        if existing_song is not None:
            db.add(
                OrderEvent(
                    order=order,
                    event_type="song_generation_autostart_reused_active_job",
                    payload={
                        "payment_public_id": payment.public_id,
                        "trigger": trigger,
                        "song_job_id": existing_song.public_id,
                        "attempt_no": existing_song.attempt_no,
                    },
                )
            )
            return existing_song

    song = create_song_job_record(db, order, queued_event_type="song_generation_autostart_enqueued", trigger=trigger)

    from app.tasks import run_song_start_task

    try:
        background_job = enqueue_background_job(
            db,
            order=order,
            job_type="song_generation_start",
            func=run_song_start_task,
            payload={
                "song_public_id": song.public_id,
                "order_public_id": order.public_id,
                "payment_public_id": payment.public_id,
                "started_event_type": "song_generation_autostart_started",
                "failed_event_type": "song_generation_autostart_failed",
                "trigger": trigger,
            },
            force_sync=True,
        )
        db.add(
            OrderEvent(
                order=order,
                event_type="song_generation_autostart_job_queued",
                payload={
                    "song_job_id": song.public_id,
                    "payment_public_id": payment.public_id,
                    "trigger": trigger,
                    "attempt_no": song.attempt_no,
                    "background_job_id": background_job.public_id,
                },
            )
        )
    except BackgroundJobError as exc:
        song.status = "failed"
        song.error_message = str(exc)
        song.started_at = utcnow()
        song.finished_at = utcnow()
        order.status = "song_failed"
        db.add(
            OrderEvent(
                order=order,
                event_type="song_generation_autostart_failed",
                payload={
                    "song_job_id": song.public_id,
                    "payment_public_id": payment.public_id,
                    "trigger": trigger,
                    "attempt_no": song.attempt_no,
                    "error": str(exc),
                },
            )
        )
    return song


def finalize_successful_payment(
    db: Session,
    payment: OrderPayment,
    *,
    trigger: str,
) -> SongGeneration | None:
    if payment.paid_at is None:
        payment.paid_at = utcnow()

    payment.order.status = "paid"
    maybe_send_payment_success_email(db, payment.order, payment)
    return maybe_start_song_generation_after_payment(
        db,
        payment.order,
        payment,
        trigger=trigger,
    )


def sync_payment_with_remote(
    db: Session,
    payment: OrderPayment,
    *,
    trigger: str,
    event_name: str | None = None,
) -> None:
    if not payment.yookassa_payment_id:
        return

    remote = fetch_payment(payment.yookassa_payment_id)

    payment.status = remote.status
    payment.confirmation_url = remote.confirmation_url
    payment.raw_payload = remote.raw

    order = payment.order

    if remote.status == "succeeded":
        finalize_successful_payment(
            db,
            payment,
            trigger=trigger,
        )
    elif remote.status == "canceled":
        order.status = "payment_canceled"
    else:
        order.status = "payment_pending"

    if event_name:
        db.add(
            OrderEvent(
                order=order,
                event_type=event_name,
                payload={
                    "payment_public_id": payment.public_id,
                    "yookassa_payment_id": payment.yookassa_payment_id,
                    "status": remote.status,
                    "trigger": trigger,
                },
            )
        )


def sync_recent_pending_payments(
    db: Session,
    *,
    trigger: str,
    created_after: datetime | None = None,
    limit: int = 200,
    event_name: str | None = None,
    failed_event_name: str | None = None,
) -> tuple[int, int]:
    query = db.query(OrderPayment).filter(
        or_(
            OrderPayment.status == "pending",
            OrderPayment.status == "waiting_for_capture",
        ),
        OrderPayment.yookassa_payment_id.isnot(None),
    )
    if created_after is not None:
        query = query.filter(OrderPayment.created_at >= created_after)
    query = query.order_by(OrderPayment.id.desc()).limit(max(1, int(limit)))

    synced = 0
    failed = 0
    for payment in query.all():
        try:
            sync_payment_with_remote(
                db,
                payment,
                trigger=trigger,
                event_name=event_name,
            )
            synced += 1
        except YooKassaError as exc:
            failed += 1
            if failed_event_name:
                db.add(
                    OrderEvent(
                        order=payment.order,
                        event_type=failed_event_name,
                        payload={
                            "payment_public_id": payment.public_id,
                            "yookassa_payment_id": payment.yookassa_payment_id,
                            "status": payment.status,
                            "trigger": trigger,
                            "error": str(exc),
                        },
                    )
                )
    return synced, failed
