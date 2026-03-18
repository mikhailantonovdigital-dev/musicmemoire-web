from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.security import utcnow
from app.models import Order, OrderEvent, OrderPayment, SongGeneration, User
from app.services.suno_service import SunoServiceError, start_song_generation
from app.services.yookassa_service import YooKassaError, fetch_payment

templates = Jinja2Templates(directory="app/templates")
templates.env.globals["settings"] = settings

router = APIRouter(prefix="/admin", tags=["admin"])

BERLIN_TZ = ZoneInfo("Europe/Berlin")
RUNNING_SONG_STATUSES = {"queued", "processing"}


def has_admin_access(request: Request) -> bool:
    if not settings.ADMIN_TOKEN:
        return True
    return bool(request.session.get("admin_access"))


def set_admin_flash(request: Request, kind: str, text: str) -> None:
    request.session["admin_flash"] = {"kind": kind, "text": text}


def pop_admin_flash(request: Request) -> dict | None:
    return request.session.pop("admin_flash", None)


def get_today_range_utc() -> tuple[datetime, datetime]:
    now_local = datetime.now(BERLIN_TZ)
    start_local = datetime.combine(now_local.date(), time.min, tzinfo=BERLIN_TZ)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def get_latest_payment(order: Order) -> OrderPayment | None:
    if not order.payments:
        return None
    return sorted(order.payments, key=lambda item: item.id or 0, reverse=True)[0]


def get_latest_song(order: Order) -> SongGeneration | None:
    if not order.song_generations:
        return None
    return sorted(order.song_generations, key=lambda item: item.id or 0, reverse=True)[0]


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


def has_successful_payment(order: Order) -> bool:
    return any(payment.status == "succeeded" for payment in order.payments)


def can_run_song(order: Order) -> bool:
    return has_successful_payment(order)


def build_order_card(order: Order) -> dict:
    latest_payment = get_latest_payment(order)
    latest_song = get_latest_song(order)
    return {
        "order": order,
        "latest_payment": latest_payment,
        "latest_song": latest_song,
        "payment_status_label": humanize_payment_status(latest_payment.status if latest_payment else None),
        "song_status_label": humanize_song_status(latest_song.status if latest_song else None),
        "can_run_song": can_run_song(order),
    }


def sync_payment_status(db: Session, payment: OrderPayment) -> None:
    remote = fetch_payment(payment.yookassa_payment_id)

    payment.status = remote.status
    payment.confirmation_url = remote.confirmation_url
    payment.raw_payload = remote.raw

    if remote.status == "succeeded":
        if payment.paid_at is None:
            payment.paid_at = utcnow()
        payment.order.status = "paid"
    elif remote.status == "canceled":
        payment.order.status = "payment_canceled"
    else:
        payment.order.status = "payment_pending"

    db.add(
        OrderEvent(
            order=payment.order,
            event_type="admin_payment_status_synced",
            payload={
                "payment_public_id": payment.public_id,
                "yookassa_payment_id": payment.yookassa_payment_id,
                "status": payment.status,
            },
        )
    )


def run_song_generation_from_admin(db: Session, order: Order) -> SongGeneration:
    latest_song = get_latest_song(order)
    if latest_song and latest_song.status in RUNNING_SONG_STATUSES:
        return latest_song

    lyrics_text = (order.final_lyrics_text or "").strip()
    if not lyrics_text:
        raise SunoServiceError("У заказа нет финального текста.")

    if order.user_id is None:
        raise SunoServiceError("У заказа нет привязки к пользователю.")

    if not can_run_song(order):
        raise SunoServiceError("Генерация песни доступна только после оплаты.")

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

    result = start_song_generation(order_number=order.order_number, lyrics_text=lyrics_text)

    song.external_job_id = result.external_job_id
    song.status = result.status
    song.started_at = utcnow()
    song.raw_payload = result.raw
    song.error_message = None
    order.status = "song_pending"

    db.add(
        OrderEvent(
            order=order,
            event_type="admin_song_generation_started",
            payload={
                "song_job_id": song.public_id,
                "attempt_no": song.attempt_no,
                "provider": song.provider,
                "external_job_id": song.external_job_id,
            },
        )
    )
    return song


@router.get("/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    if has_admin_access(request):
        return RedirectResponse(url="/admin/", status_code=303)

    return templates.TemplateResponse(
        "admin/login.html",
        {
            "request": request,
            "page_title": "Вход в админку",
            "error": None,
        },
    )


@router.post("/login", response_class=HTMLResponse)
async def admin_login_submit(
    request: Request,
    token: str = Form(...),
):
    if not settings.ADMIN_TOKEN:
        return RedirectResponse(url="/admin/", status_code=303)

    if token.strip() != settings.ADMIN_TOKEN:
        return templates.TemplateResponse(
            "admin/login.html",
            {
                "request": request,
                "page_title": "Вход в админку",
                "error": "Неверный токен.",
            },
            status_code=400,
        )

    request.session["admin_access"] = True
    return RedirectResponse(url="/admin/", status_code=303)


@router.get("/logout")
async def admin_logout(request: Request):
    request.session.pop("admin_access", None)
    return RedirectResponse(url="/", status_code=303)


@router.get("/", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    q: str | None = None,
    db: Session = Depends(get_db),
):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)

    query_text = (q or "").strip()
    day_start_utc, day_end_utc = get_today_range_utc()

    new_users_today = (
        db.query(func.count(User.id))
        .filter(User.created_at >= day_start_utc, User.created_at < day_end_utc)
        .scalar()
        or 0
    )
    total_users = db.query(func.count(User.id)).scalar() or 0
    orders_today = (
        db.query(func.count(Order.id))
        .filter(Order.created_at >= day_start_utc, Order.created_at < day_end_utc)
        .scalar()
        or 0
    )
    total_paid_orders = (
        db.query(func.count(func.distinct(OrderPayment.order_id)))
        .filter(OrderPayment.status == "succeeded")
        .scalar()
        or 0
    )
    failed_song_jobs = (
        db.query(func.count(SongGeneration.id))
        .filter(SongGeneration.status == "failed")
        .scalar()
        or 0
    )
    pending_payments = (
        db.query(func.count(OrderPayment.id))
        .filter(OrderPayment.status.in_(["pending", "waiting_for_capture"]))
        .scalar()
        or 0
    )

    orders_query = db.query(Order).outerjoin(User, Order.user_id == User.id)

    if query_text:
        pattern = f"%{query_text}%"
        orders_query = orders_query.filter(
            or_(
                Order.order_number.ilike(pattern),
                Order.public_id.ilike(pattern),
                User.email.ilike(pattern),
            )
        )

    orders = orders_query.order_by(Order.id.desc()).limit(50).all()

    order_status_counts = (
        db.query(Order.status, func.count(Order.id))
        .group_by(Order.status)
        .order_by(func.count(Order.id).desc())
        .all()
    )
    payment_status_counts = (
        db.query(OrderPayment.status, func.count(OrderPayment.id))
        .group_by(OrderPayment.status)
        .order_by(func.count(OrderPayment.id).desc())
        .all()
    )
    song_status_counts = (
        db.query(SongGeneration.status, func.count(SongGeneration.id))
        .group_by(SongGeneration.status)
        .order_by(func.count(SongGeneration.id).desc())
        .all()
    )

    order_cards = [build_order_card(order) for order in orders]

    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "page_title": "Админка",
            "flash": pop_admin_flash(request),
            "q": query_text,
            "admin_token_enabled": bool(settings.ADMIN_TOKEN),
            "new_users_today": new_users_today,
            "total_users": total_users,
            "orders_today": orders_today,
            "total_paid_orders": total_paid_orders,
            "failed_song_jobs": failed_song_jobs,
            "pending_payments": pending_payments,
            "order_status_counts": order_status_counts,
            "payment_status_counts": payment_status_counts,
            "song_status_counts": song_status_counts,
            "order_cards": order_cards,
        },
    )


@router.get("/orders/{order_public_id}", response_class=HTMLResponse)
async def admin_order_detail(
    order_public_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)

    order = db.query(Order).filter(Order.public_id == order_public_id).first()
    if order is None:
        set_admin_flash(request, "error", "Заказ не найден.")
        return RedirectResponse(url="/admin/", status_code=303)

    latest_payment = get_latest_payment(order)
    latest_song = get_latest_song(order)
    events = (
        db.query(OrderEvent)
        .filter(OrderEvent.order_id == order.id)
        .order_by(OrderEvent.id.desc())
        .limit(30)
        .all()
    )

    return templates.TemplateResponse(
        "admin/order_detail.html",
        {
            "request": request,
            "page_title": f"Админка · {order.order_number}",
            "flash": pop_admin_flash(request),
            "admin_token_enabled": bool(settings.ADMIN_TOKEN),
            "order": order,
            "latest_payment": latest_payment,
            "latest_song": latest_song,
            "payment_status_label": humanize_payment_status(latest_payment.status if latest_payment else None),
            "song_status_label": humanize_song_status(latest_song.status if latest_song else None),
            "can_run_song": can_run_song(order),
            "events": events,
        },
    )


@router.post("/orders/{order_public_id}/payment-sync")
async def admin_order_payment_sync(
    order_public_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)

    order = db.query(Order).filter(Order.public_id == order_public_id).first()
    if order is None:
        set_admin_flash(request, "error", "Заказ не найден.")
        return RedirectResponse(url="/admin/", status_code=303)

    latest_payment = get_latest_payment(order)
    if latest_payment is None or not latest_payment.yookassa_payment_id:
        set_admin_flash(request, "warning", "У заказа нет платежа для синхронизации.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    try:
        sync_payment_status(db, latest_payment)
        db.commit()
    except YooKassaError as exc:
        db.rollback()
        set_admin_flash(request, "error", str(exc))
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    set_admin_flash(request, "success", "Статус оплаты обновлён.")
    return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)


@router.post("/orders/{order_public_id}/song-run")
async def admin_order_song_run(
    order_public_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)

    order = db.query(Order).filter(Order.public_id == order_public_id).first()
    if order is None:
        set_admin_flash(request, "error", "Заказ не найден.")
        return RedirectResponse(url="/admin/", status_code=303)

    try:
        song = run_song_generation_from_admin(db, order)
        db.commit()
    except SunoServiceError as exc:
        db.rollback()
        set_admin_flash(request, "error", str(exc))
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    set_admin_flash(request, "success", f"Генерация песни запущена. Попытка #{song.attempt_no}.")
    return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)
