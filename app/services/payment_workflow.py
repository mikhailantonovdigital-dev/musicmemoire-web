from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import utcnow
from app.models import Order, OrderEvent, OrderPayment, SongGeneration
from app.services.email_service import EmailServiceError, send_payment_success_email
from app.services.suno_service import SunoServiceError, start_song_generation
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


def maybe_send_payment_success_email(db: Session, order: Order, payment: OrderPayment) -> None:
    if order.user is None or not order.user.email:
        return

    if has_order_event(db, order, "payment_success_email_sent"):
        return

    order_url = f"{settings.BASE_URL.rstrip('/')}/account/orders/{order.public_id}"

    try:
        send_payment_success_email(
            recipient_email=order.user.email,
            order_number=order.order_number,
            order_url=order_url,
            price_rub=payment.final_amount_rub,
        )
        db.add(
            OrderEvent(
                order=order,
                event_type="payment_success_email_sent",
                payload={
                    "payment_public_id": payment.public_id,
                    "email": order.user.email,
                },
            )
        )
    except EmailServiceError as exc:
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

def resend_payment_success_email(db: Session, order: Order, payment: OrderPayment) -> None:
    if order.user is None or not order.user.email:
        raise EmailServiceError("У заказа нет email для отправки письма.")

    order_url = f"{settings.BASE_URL.rstrip('/')}/account/orders/{order.public_id}"

    try:
        send_payment_success_email(
            recipient_email=order.user.email,
            order_number=order.order_number,
            order_url=order_url,
            price_rub=payment.final_amount_rub,
        )
        db.add(
            OrderEvent(
                order=order,
                event_type="payment_success_email_resent",
                payload={
                    "payment_public_id": payment.public_id,
                    "email": order.user.email,
                    "trigger": "admin_manual_resend",
                },
            )
        )
    except EmailServiceError as exc:
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
        raise


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

    try:
        result = start_song_generation(
            order_number=order.order_number,
            lyrics_text=lyrics_text,
            song_style=order.song_style,
            song_style_custom=order.song_style_custom,
            singer_gender=order.singer_gender,
        )
    except SunoServiceError as exc:
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
                    "attempt_no": attempt_no,
                    "error": str(exc),
                },
            )
        )
        return song

    song.external_job_id = result.external_job_id
    song.status = result.status
    song.started_at = utcnow()
    song.raw_payload = result.raw
    song.error_message = None
    order.status = "song_pending"

    db.add(
        OrderEvent(
            order=order,
            event_type="song_generation_autostart_started",
            payload={
                "song_job_id": song.public_id,
                "payment_public_id": payment.public_id,
                "trigger": trigger,
                "attempt_no": song.attempt_no,
                "provider": song.provider,
                "external_job_id": song.external_job_id,
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
