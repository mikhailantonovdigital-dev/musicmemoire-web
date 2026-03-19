from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.security import utcnow
from app.models import EmailLog, Order, User


EMAIL_TYPE_LABELS = {
    "magic_link": "Magic link",
    "payment_success": "Оплата",
    "song_ready": "Песня готова",
    "song_failed": "Ошибка заказа",
}

EMAIL_STATUS_LABELS = {
    "sent": "Отправлено",
    "stub": "Stub",
    "failed": "Ошибка",
}


def humanize_email_type(value: str | None) -> str:
    return EMAIL_TYPE_LABELS.get((value or "").strip(), value or "—")



def humanize_email_status(value: str | None) -> str:
    return EMAIL_STATUS_LABELS.get((value or "").strip(), value or "—")



def create_email_log(
    db: Session,
    *,
    email_type: str,
    recipient_email: str,
    subject: str,
    status: str,
    delivery_mode: str,
    order: Order | None = None,
    user: User | None = None,
    background_job_public_id: str | None = None,
    payload: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> EmailLog:
    sent_at = utcnow() if status in {"sent", "stub"} else None
    log = EmailLog(
        order_id=order.id if order is not None else None,
        user_id=user.id if user is not None else (order.user_id if order is not None else None),
        background_job_public_id=background_job_public_id,
        email_type=email_type,
        recipient_email=recipient_email,
        subject=subject,
        delivery_mode=delivery_mode,
        status=status,
        error_message=error_message,
        payload=payload,
        sent_at=sent_at,
    )
    db.add(log)
    return log
