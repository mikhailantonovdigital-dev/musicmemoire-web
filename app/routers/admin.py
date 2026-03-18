from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.templates import templates
from app.models import LyricsVersion, Order, OrderEvent, OrderPayment, SongGeneration, User
from app.core.security import utcnow
from app.services.song_workflow import (
    RUNNING_SONG_STATUSES,
    create_song_job,
    get_latest_song,
    has_successful_payment,
    humanize_song_status,
    resend_song_ready_email,
    sync_song_job_state,
)
from app.services.email_service import EmailServiceError
from app.services.payment_workflow import resend_payment_success_email, sync_payment_with_remote
from app.services.suno_service import SunoServiceError
from app.services.yookassa_service import YooKassaError

router = APIRouter(prefix="/admin", tags=["admin"])

BERLIN_TZ = ZoneInfo("Europe/Berlin")

ORDER_STATUS_OPTIONS = [
    ("draft", "draft"),
    ("awaiting_payment", "awaiting_payment"),
    ("payment_pending", "payment_pending"),
    ("payment_canceled", "payment_canceled"),
    ("paid", "paid"),
    ("song_pending", "song_pending"),
    ("song_ready", "song_ready"),
    ("song_failed", "song_failed"),
]

SONG_STATUS_OPTIONS = [
    ("queued", "queued"),
    ("processing", "processing"),
    ("succeeded", "succeeded"),
    ("failed", "failed"),
    ("canceled", "canceled"),
]

VALID_ORDER_STATUSES = {value for value, _label in ORDER_STATUS_OPTIONS}
VALID_SONG_STATUSES = {value for value, _label in SONG_STATUS_OPTIONS}


def normalize_multiline_urls(value: str | None) -> list[str]:
    if not value:
        return []
    items: list[str] = []
    normalized = value.replace("\r", "\n")
    for line in normalized.split("\n"):
        item = line.strip()
        if item:
            items.append(item)
    return items


def build_manual_result_tracks(urls: list[str]) -> list[dict]:
    tracks: list[dict] = []
    for index, url in enumerate(urls, start=1):
        tracks.append({
            "id": f"manual-{index}",
            "title": f"Вариант {index}",
            "audio_url": url,
            "stream_audio_url": url,
            "source": "admin_manual",
        })
    return tracks



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


def get_sorted_lyrics_versions(order: Order) -> list[LyricsVersion]:
    if not order.lyrics_versions:
        return []
    return sorted(order.lyrics_versions, key=lambda item: item.id or 0)


def get_selected_lyrics_version(order: Order) -> LyricsVersion | None:
    versions = get_sorted_lyrics_versions(order)
    for version in versions:
        if version.is_selected:
            return version
    return versions[-1] if versions else None


def humanize_payment_status(status: str | None) -> str:
    mapping = {
        "pending": "Ожидает оплаты",
        "waiting_for_capture": "Ожидает подтверждения",
        "succeeded": "Оплачено",
        "canceled": "Не оплачено",
    }
    return mapping.get(status or "", "Не начата")


def can_run_song(order: Order) -> bool:
    return has_successful_payment(order)


def can_resend_payment_email(order: Order) -> bool:
    latest_payment = get_latest_payment(order)
    return bool(latest_payment and latest_payment.status == "succeeded" and order.user and order.user.email)


def can_resend_song_ready_email(order: Order) -> bool:
    latest_song = get_latest_song(order)
    return bool(latest_song and latest_song.status == "succeeded" and order.user and order.user.email)


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
        "can_resend_payment_email": can_resend_payment_email(order),
        "can_resend_song_ready_email": can_resend_song_ready_email(order),
    }


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
async def admin_login_submit(request: Request, token: str = Form(...)):
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
async def admin_dashboard(request: Request, q: str | None = None, db: Session = Depends(get_db)):
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
async def admin_order_detail(order_public_id: str, request: Request, db: Session = Depends(get_db)):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)

    order = db.query(Order).filter(Order.public_id == order_public_id).first()
    if order is None:
        set_admin_flash(request, "error", "Заказ не найден.")
        return RedirectResponse(url="/admin/", status_code=303)

    latest_payment = get_latest_payment(order)
    latest_song = get_latest_song(order)
    lyrics_versions = get_sorted_lyrics_versions(order)
    selected_lyrics_version = get_selected_lyrics_version(order)
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
            "lyrics_versions": lyrics_versions,
            "selected_lyrics_version": selected_lyrics_version,
            "payment_status_label": humanize_payment_status(latest_payment.status if latest_payment else None),
            "song_status_label": humanize_song_status(latest_song.status if latest_song else None),
            "can_run_song": can_run_song(order),
            "can_resend_payment_email": can_resend_payment_email(order),
            "can_resend_song_ready_email": can_resend_song_ready_email(order),
            "order_status_options": ORDER_STATUS_OPTIONS,
            "song_status_options": SONG_STATUS_OPTIONS,
            "events": events,
        },
    )


@router.post("/orders/{order_public_id}/payment-sync")
async def admin_order_payment_sync(order_public_id: str, request: Request, db: Session = Depends(get_db)):
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
        sync_payment_with_remote(
            db,
            latest_payment,
            trigger="admin_manual_sync",
            event_name="admin_payment_status_synced",
        )
        db.commit()
    except YooKassaError as exc:
        db.rollback()
        set_admin_flash(request, "error", str(exc))
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    set_admin_flash(request, "success", "Статус оплаты обновлён.")
    return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)


@router.post("/orders/{order_public_id}/song-run")
async def admin_order_song_run(order_public_id: str, request: Request, db: Session = Depends(get_db)):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)

    order = db.query(Order).filter(Order.public_id == order_public_id).first()
    if order is None:
        set_admin_flash(request, "error", "Заказ не найден.")
        return RedirectResponse(url="/admin/", status_code=303)

    try:
        song = create_song_job(db, order, event_type="admin_song_generation_started")
        db.commit()
        db.refresh(song)
    except SunoServiceError as exc:
        db.rollback()
        set_admin_flash(request, "error", str(exc))
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    set_admin_flash(request, "success", f"Генерация песни запущена. Попытка #{song.attempt_no}.")
    return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)


@router.post("/orders/{order_public_id}/song-sync")
async def admin_order_song_sync(order_public_id: str, request: Request, db: Session = Depends(get_db)):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)

    order = db.query(Order).filter(Order.public_id == order_public_id).first()
    if order is None:
        set_admin_flash(request, "error", "Заказ не найден.")
        return RedirectResponse(url="/admin/", status_code=303)

    latest_song = get_latest_song(order)
    if latest_song is None or not latest_song.external_job_id:
        set_admin_flash(request, "warning", "У заказа нет задачи генерации для синхронизации.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    if latest_song.status not in RUNNING_SONG_STATUSES:
        set_admin_flash(request, "warning", "Статус песни уже финальный. Синхронизация не нужна.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    try:
        latest_song = sync_song_job_state(db, latest_song, event_type="admin_song_status_synced")
        db.commit()
        db.refresh(latest_song)
    except SunoServiceError as exc:
        db.rollback()
        set_admin_flash(request, "error", str(exc))
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    if latest_song.status == "succeeded":
        set_admin_flash(request, "success", "Песня готова. Результат обновлён.")
    elif latest_song.status == "failed":
        set_admin_flash(request, "error", latest_song.error_message or "Генерация завершилась ошибкой.")
    else:
        set_admin_flash(request, "success", "Статус песни обновлён. Генерация ещё продолжается.")

    return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)


@router.post("/orders/{order_public_id}/status-update")
async def admin_order_status_update(
    order_public_id: str,
    request: Request,
    status: str = Form(...),
    db: Session = Depends(get_db),
):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)

    order = db.query(Order).filter(Order.public_id == order_public_id).first()
    if order is None:
        set_admin_flash(request, "error", "Заказ не найден.")
        return RedirectResponse(url="/admin/", status_code=303)

    target_status = (status or "").strip()
    if target_status not in VALID_ORDER_STATUSES:
        set_admin_flash(request, "error", "Недопустимый статус заказа.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    previous_status = order.status
    if previous_status == target_status:
        set_admin_flash(request, "warning", "Статус заказа уже такой.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    order.status = target_status
    db.add(
        OrderEvent(
            order=order,
            event_type="admin_order_status_changed",
            payload={
                "status_from": previous_status,
                "status_to": target_status,
                "trigger": "admin_manual_status_change",
            },
        )
    )
    db.commit()

    set_admin_flash(request, "success", f"Статус заказа изменён: {previous_status} → {target_status}.")
    return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)


@router.post("/orders/{order_public_id}/song-status-update")
async def admin_order_song_status_update(
    order_public_id: str,
    request: Request,
    song_status: str = Form(...),
    audio_urls: str = Form(""),
    error_message: str = Form(""),
    db: Session = Depends(get_db),
):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)

    order = db.query(Order).filter(Order.public_id == order_public_id).first()
    if order is None:
        set_admin_flash(request, "error", "Заказ не найден.")
        return RedirectResponse(url="/admin/", status_code=303)

    song = get_latest_song(order)
    if song is None:
        set_admin_flash(request, "warning", "У заказа нет задачи песни для ручного обновления.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    target_status = (song_status or "").strip()
    if target_status not in VALID_SONG_STATUSES:
        set_admin_flash(request, "error", "Недопустимый статус песни.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    urls = normalize_multiline_urls(audio_urls)
    manual_error = (error_message or "").strip()

    previous_status = song.status
    previous_order_status = order.status

    song.status = target_status

    if target_status in {"queued", "processing"}:
        if song.started_at is None:
            song.started_at = utcnow()
        song.finished_at = None
        order.status = "song_pending"
        if manual_error:
            song.error_message = manual_error
        elif target_status != "failed":
            song.error_message = None
    elif target_status == "succeeded":
        if song.started_at is None:
            song.started_at = utcnow()
        song.finished_at = utcnow()
        order.status = "song_ready"
        song.error_message = None
    elif target_status == "failed":
        if song.started_at is None:
            song.started_at = utcnow()
        song.finished_at = utcnow()
        order.status = "song_failed"
        song.error_message = manual_error or song.error_message or "Статус вручную переведён в failed оператором."
    else:  # canceled
        if song.started_at is None:
            song.started_at = utcnow()
        song.finished_at = utcnow()
        order.status = "paid"
        song.error_message = manual_error or song.error_message

    if urls:
        song.audio_url = urls[0]
        song.result_tracks = build_manual_result_tracks(urls)
    elif target_status == "succeeded" and song.audio_url:
        if not song.result_tracks:
            song.result_tracks = build_manual_result_tracks([song.audio_url])

    db.add(
        OrderEvent(
            order=order,
            event_type="admin_song_status_changed",
            payload={
                "song_job_id": song.public_id,
                "status_from": previous_status,
                "status_to": target_status,
                "order_status_from": previous_order_status,
                "order_status_to": order.status,
                "audio_url_count": len(urls),
                "has_error_message": bool(song.error_message),
                "trigger": "admin_manual_status_change",
            },
        )
    )
    db.commit()

    set_admin_flash(request, "success", f"Статус песни изменён: {previous_status} → {target_status}.")
    return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)


@router.post("/orders/{order_public_id}/final-lyrics-update")
async def admin_order_final_lyrics_update(
    order_public_id: str,
    request: Request,
    final_lyrics_text: str = Form(default=""),
    db: Session = Depends(get_db),
):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)

    order = db.query(Order).filter(Order.public_id == order_public_id).first()
    if order is None:
        set_admin_flash(request, "error", "Заказ не найден.")
        return RedirectResponse(url="/admin/", status_code=303)

    value = (final_lyrics_text or "").strip()
    if not value:
        set_admin_flash(request, "error", "Финальный текст не может быть пустым.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    previous_value = (order.final_lyrics_text or "").strip()
    if previous_value == value:
        set_admin_flash(request, "warning", "Финальный текст уже такой.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    order.final_lyrics_text = value

    selected_version = get_selected_lyrics_version(order)
    synced_selected_version = False
    if selected_version is not None:
        selected_version.edited_lyrics_text = value
        synced_selected_version = True

    db.add(
        OrderEvent(
            order=order,
            event_type="admin_final_lyrics_updated",
            payload={
                "previous_length": len(previous_value),
                "new_length": len(value),
                "lyrics_mode": order.lyrics_mode,
                "selected_version_public_id": selected_version.public_id if selected_version else None,
                "selected_version_synced": synced_selected_version,
                "trigger": "admin_manual_edit",
            },
        )
    )
    db.commit()

    set_admin_flash(request, "success", "Финальный текст сохранён. Следующий запуск песни возьмёт уже новую версию.")
    return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)


@router.post("/orders/{order_public_id}/payment-email-resend")
async def admin_order_payment_email_resend(order_public_id: str, request: Request, db: Session = Depends(get_db)):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)

    order = db.query(Order).filter(Order.public_id == order_public_id).first()
    if order is None:
        set_admin_flash(request, "error", "Заказ не найден.")
        return RedirectResponse(url="/admin/", status_code=303)

    latest_payment = get_latest_payment(order)
    if latest_payment is None or latest_payment.status != "succeeded":
        set_admin_flash(request, "warning", "Письмо об оплате можно отправить только для успешно оплаченного заказа.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    try:
        resend_payment_success_email(db, order, latest_payment)
        db.commit()
    except EmailServiceError as exc:
        db.rollback()
        set_admin_flash(request, "error", str(exc))
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    set_admin_flash(request, "success", "Письмо об успешной оплате отправлено повторно.")
    return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)


@router.post("/orders/{order_public_id}/song-ready-email-resend")
async def admin_order_song_ready_email_resend(order_public_id: str, request: Request, db: Session = Depends(get_db)):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)

    order = db.query(Order).filter(Order.public_id == order_public_id).first()
    if order is None:
        set_admin_flash(request, "error", "Заказ не найден.")
        return RedirectResponse(url="/admin/", status_code=303)

    latest_song = get_latest_song(order)
    if latest_song is None or latest_song.status != "succeeded":
        set_admin_flash(request, "warning", "Письмо о готовой песне можно отправить только после успешной генерации.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    try:
        resend_song_ready_email(db, latest_song)
        db.commit()
    except EmailServiceError as exc:
        db.rollback()
        set_admin_flash(request, "error", str(exc))
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    set_admin_flash(request, "success", "Письмо о готовой песне отправлено повторно.")
    return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)
