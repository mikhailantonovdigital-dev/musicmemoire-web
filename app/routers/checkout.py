from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.security import get_session_user
from app.core.templates import templates
from app.models import Order, OrderEvent, OrderPayment
from app.models.order_payment import build_order_pricing_preview
from app.services.payment_workflow import (
    FINAL_PAYMENT_STATUSES,
    PENDING_PAYMENT_STATUSES,
    finalize_successful_payment,
    get_latest_song,
    sync_payment_with_remote,
)
from app.services.yookassa_service import YooKassaError, create_redirect_payment, fetch_payment

router = APIRouter(prefix="/checkout", tags=["checkout"])


def get_checkout_order(request: Request, db: Session, order_public_id: str) -> Order | None:
    order = db.query(Order).filter(Order.public_id == order_public_id).first()
    if order is None:
        return None

    draft_order_id = request.session.get("draft_order_id")
    if draft_order_id and int(draft_order_id) == order.id:
        return order

    user = get_session_user(request, db)
    if user and order.user_id == user.id:
        return order

    return None


def get_checkout_payment(request: Request, db: Session, payment_public_id: str) -> OrderPayment | None:
    payment = db.query(OrderPayment).filter(OrderPayment.public_id == payment_public_id).first()
    if payment is None:
        return None

    if get_checkout_order(request, db, payment.order.public_id) is None:
        return None

    return payment


def humanize_payment_status(status: str | None) -> str:
    mapping = {
        "pending": "Ожидает оплаты",
        "waiting_for_capture": "Ожидает подтверждения",
        "succeeded": "Оплачено",
        "canceled": "Не оплачено",
    }
    return mapping.get(status or "", "Не начата")



def humanize_song_status(status: str | None) -> str:
    mapping = {
        "queued": "В очереди",
        "processing": "Генерируется",
        "succeeded": "Готово",
        "failed": "Ошибка",
        "canceled": "Отменено",
    }
    return mapping.get(status or "", "Не запускалась")



def build_payment_template_context(payment: OrderPayment) -> dict[str, int | bool]:
    return {
        "price_rub": payment.final_amount_rub,
        "base_price_rub": payment.base_amount_rub,
        "discount_rub": payment.discount_amount_rub,
        "has_discount": payment.has_discount,
    }


async def _checkout_start(order_public_id: str, request: Request, db: Session):
    order = get_checkout_order(request, db, order_public_id)
    if order is None:
        raise HTTPException(status_code=403, detail="Нет доступа к заказу.")

    if not (order.final_lyrics_text or "").strip():
        return RedirectResponse(url="/questionnaire/lyrics", status_code=303)

    if order.user_id is None:
        return RedirectResponse(url="/questionnaire/access", status_code=303)

    latest_payment = order.payments[0] if order.payments else None
    if latest_payment and latest_payment.status in PENDING_PAYMENT_STATUSES and latest_payment.confirmation_url:
        return RedirectResponse(url=latest_payment.confirmation_url, status_code=303)

    if latest_payment and latest_payment.status == "succeeded":
        return RedirectResponse(url=f"/checkout/status?payment={latest_payment.public_id}", status_code=303)

    pricing = build_order_pricing_preview(db, order)

    payment = OrderPayment(
        order_id=order.id,
        user_id=order.user_id,
        provider="yookassa",
        status="pending",
        amount_value=pricing["final_amount_value"],
        base_amount_value=pricing["base_amount_value"],
        discount_amount_value=pricing["discount_amount_value"],
        final_amount_value=pricing["final_amount_value"],
        currency="RUB",
    )
    db.add(payment)
    db.flush()

    return_url = f"{settings.BASE_URL.rstrip('/')}/checkout/status?payment={payment.public_id}"
    payment.return_url = return_url

    try:
        result = create_redirect_payment(
            order_number=order.order_number,
            order_public_id=order.public_id,
            user_public_id=order.user.public_id if order.user else None,
            payment_public_id=payment.public_id,
            amount_rub=payment.final_amount_rub,
            return_url=return_url,
            customer_email=order.user.email if order.user and order.user.email else None,
        )
    except YooKassaError as exc:
        db.add(
            OrderEvent(
                order=order,
                event_type="payment_create_failed",
                payload={
                    "payment_public_id": payment.public_id,
                    "error": str(exc),
                },
            )
        )
        db.commit()
        return templates.TemplateResponse(
            "checkout/status.html",
            {
                "request": request,
                "page_title": "Оплата",
                "payment": payment,
                "order": order,
                "latest_song": get_latest_song(order),
                "payment_status_label": humanize_payment_status(payment.status),
                "song_status_label": humanize_song_status(None),
                "is_success": False,
                "is_pending": False,
                "is_canceled": False,
                "error": str(exc),
                "metrica_counter_id": settings.METRICA_COUNTER_ID,
                **build_payment_template_context(payment),
            },
            status_code=400,
        )

    payment.yookassa_payment_id = result.payment.id
    payment.idempotence_key = result.idempotence_key
    payment.status = result.payment.status
    payment.confirmation_url = result.payment.confirmation_url
    payment.raw_payload = result.payment.raw
    order.status = "paid" if result.payment.status == "succeeded" else "payment_pending"

    if result.payment.status == "succeeded":
        finalize_successful_payment(
            db,
            payment,
            trigger="payment_create_response",
        )

    db.add(
        OrderEvent(
            order=order,
            event_type="payment_created",
            payload={
                "payment_public_id": payment.public_id,
                "yookassa_payment_id": payment.yookassa_payment_id,
                "status": payment.status,
                "amount_value": payment.amount_value,
                "base_amount_value": payment.base_amount_value,
                "discount_amount_value": payment.discount_amount_value,
                "final_amount_value": payment.final_amount_value,
            },
        )
    )
    db.commit()

    if payment.status == "succeeded":
        return RedirectResponse(url=f"/checkout/status?payment={payment.public_id}", status_code=303)

    if not payment.confirmation_url:
        raise HTTPException(status_code=500, detail="ЮKassa не вернула ссылку на оплату.")

    return RedirectResponse(url=payment.confirmation_url, status_code=303)


@router.get("/start/{order_public_id}")
async def checkout_start_get(order_public_id: str, request: Request, db: Session = Depends(get_db)):
    return await _checkout_start(order_public_id, request, db)


@router.post("/start/{order_public_id}")
async def checkout_start_post(order_public_id: str, request: Request, db: Session = Depends(get_db)):
    return await _checkout_start(order_public_id, request, db)


@router.get("/status", response_class=HTMLResponse)
async def checkout_status(payment: str, request: Request, db: Session = Depends(get_db)):
    payment_obj = get_checkout_payment(request, db, payment)
    if payment_obj is None:
        raise HTTPException(status_code=404, detail="Платёж не найден.")

    status_sync_error = None
    if payment_obj.yookassa_payment_id and payment_obj.status not in FINAL_PAYMENT_STATUSES:
        try:
            sync_payment_with_remote(
                db,
                payment_obj,
                trigger="status_page_sync",
                event_name="payment_status_synced_from_status_page",
            )
            db.commit()
            db.refresh(payment_obj)
        except YooKassaError as exc:
            status_sync_error = str(exc)
            db.rollback()

            payment_obj = get_checkout_payment(request, db, payment)
            if payment_obj is not None:
                db.add(
                    OrderEvent(
                        order=payment_obj.order,
                        event_type="payment_status_sync_failed",
                        payload={
                            "payment_public_id": payment_obj.public_id,
                            "yookassa_payment_id": payment_obj.yookassa_payment_id,
                            "error": status_sync_error,
                        },
                    )
                )
                db.commit()
                payment_obj = get_checkout_payment(request, db, payment)

    if payment_obj is not None and payment_obj.status == "succeeded":
        finalize_successful_payment(
            db,
            payment_obj,
            trigger="status_page_open",
        )
        db.commit()
        db.refresh(payment_obj)

    latest_song = get_latest_song(payment_obj.order)

    return templates.TemplateResponse(
        "checkout/status.html",
        {
            "request": request,
            "page_title": "Статус оплаты",
            "payment": payment_obj,
            "order": payment_obj.order,
            "latest_song": latest_song,
            "payment_status_label": humanize_payment_status(payment_obj.status),
            "song_status_label": humanize_song_status(latest_song.status if latest_song else None),
            "is_success": payment_obj.status == "succeeded",
            "is_pending": payment_obj.status in PENDING_PAYMENT_STATUSES,
            "is_canceled": payment_obj.status == "canceled",
            "error": status_sync_error,
            "metrica_counter_id": settings.METRICA_COUNTER_ID,
            **build_payment_template_context(payment_obj),
        },
    )


@router.post("/webhook/yookassa")
async def checkout_yookassa_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.json()
    event = payload.get("event")
    obj = payload.get("object") or {}
    payment_id = obj.get("id")
    webhook_status = obj.get("status")

    if not event or not payment_id:
        raise HTTPException(status_code=400, detail="Некорректное уведомление ЮKassa.")

    payment = db.query(OrderPayment).filter(OrderPayment.yookassa_payment_id == payment_id).first()
    if payment is None:
        return JSONResponse({"ok": True, "ignored": "payment_not_found"})

    try:
        remote = fetch_payment(payment_id)
    except YooKassaError as exc:
        db.add(
            OrderEvent(
                order=payment.order,
                event_type="payment_webhook_sync_failed",
                payload={
                    "payment_public_id": payment.public_id,
                    "yookassa_payment_id": payment.yookassa_payment_id,
                    "event": event,
                    "webhook_status": webhook_status,
                    "error": str(exc),
                },
            )
        )
        db.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if remote.id != payment_id or remote.status != webhook_status:
        db.add(
            OrderEvent(
                order=payment.order,
                event_type="payment_webhook_rejected",
                payload={
                    "payment_public_id": payment.public_id,
                    "yookassa_payment_id": payment.yookassa_payment_id,
                    "event": event,
                    "webhook_status": webhook_status,
                    "remote_status": remote.status,
                },
            )
        )
        db.commit()
        raise HTTPException(status_code=400, detail="Не удалось подтвердить статус платежа.")

    payment.status = remote.status
    payment.confirmation_url = remote.confirmation_url
    payment.raw_payload = remote.raw

    if remote.status == "succeeded":
        finalize_successful_payment(
            db,
            payment,
            trigger="yookassa_webhook",
        )
    elif remote.status == "canceled":
        payment.order.status = "payment_canceled"
    else:
        payment.order.status = "payment_pending"

    db.add(
        OrderEvent(
            order=payment.order,
            event_type="payment_webhook_received",
            payload={
                "payment_public_id": payment.public_id,
                "yookassa_payment_id": payment.yookassa_payment_id,
                "event": event,
                "webhook_status": webhook_status,
                "status": remote.status,
            },
        )
    )
    db.commit()

    return JSONResponse({"ok": True})
