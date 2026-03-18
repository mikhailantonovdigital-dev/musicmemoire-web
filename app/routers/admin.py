from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.security import utcnow
from app.core.templates import templates
from app.models import LyricsVersion, Order, OrderEvent, OrderPayment, SongGeneration, User, VoiceInput
from app.models.order_payment import build_order_pricing_preview
from app.services.email_service import EmailServiceError
from app.services.lyrics_generation_service import DualGenerationResult, LyricsGenerationError, generate_dual_lyrics_versions
from app.services.payment_workflow import resend_payment_success_email, sync_payment_with_remote
from app.services.song_workflow import (
    RUNNING_SONG_STATUSES,
    create_song_job,
    get_latest_ready_song,
    get_latest_song,
    get_song_attempts,
    has_successful_payment,
    humanize_song_status,
    resend_song_ready_email,
    sync_song_job_state,
)
from app.services.suno_service import SunoServiceError
from app.services.transcription_service import TranscriptionServiceError, transcribe_audio_file
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

FILTER_ALL = "all"
STORY_SOURCE_OPTIONS = [
    (FILTER_ALL, "Любой источник"),
    ("text", "Текст"),
    ("voice", "Голос"),
]
LYRICS_MODE_OPTIONS = [
    (FILTER_ALL, "Любой режим"),
    ("generate", "Генерация"),
    ("custom", "Свой текст"),
]
PAYMENT_STATUS_FILTER_OPTIONS = [
    (FILTER_ALL, "Любой статус оплаты"),
    ("pending", "pending"),
    ("waiting_for_capture", "waiting_for_capture"),
    ("succeeded", "succeeded"),
    ("canceled", "canceled"),
    ("missing", "Без платежа"),
]
SONG_STATUS_FILTER_OPTIONS = [(FILTER_ALL, "Любой статус песни"), *SONG_STATUS_OPTIONS, ("missing", "Без задачи")]
ORDER_STATUS_FILTER_OPTIONS = [(FILTER_ALL, "Любой статус заказа"), *ORDER_STATUS_OPTIONS]

VALID_ORDER_STATUSES = {value for value, _label in ORDER_STATUS_OPTIONS}
VALID_SONG_STATUSES = {value for value, _label in SONG_STATUS_OPTIONS}
FUNNEL_STAGES = [
    ("Черновики", ["draft"]),
    ("Ждут оплату", ["awaiting_payment", "payment_pending", "payment_canceled"]),
    ("Оплачены", ["paid"]),
    ("Песня в работе", ["song_pending"]),
    ("Песня готова", ["song_ready"]),
    ("Ошибка", ["song_failed"]),
]


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

def format_size(size_bytes: int | None) -> str | None:
    if size_bytes is None:
        return None
    return f"{size_bytes / (1024 * 1024):.2f} МБ"


def humanize_transcription_status(status: str | None) -> str:
    mapping = {
        "uploaded": "Загружено",
        "transcribing": "Распознаём",
        "done": "Расшифровано",
        "failed": "Ошибка распознавания",
    }
    return mapping.get(status or "", "—")


def get_voice_inputs(db: Session, order_id: int) -> list[VoiceInput]:
    return (
        db.query(VoiceInput)
        .filter(VoiceInput.order_id == order_id)
        .order_by(VoiceInput.id.desc())
        .all()
    )


def build_voice_cards(request: Request, voice_inputs: list[VoiceInput]) -> list[dict]:
    cards: list[dict] = []
    for voice in voice_inputs:
        cards.append({
            "voice": voice,
            "size_label": format_size(voice.size_bytes),
            "status_label": humanize_transcription_status(voice.transcription_status),
            "stream_url": str(request.url_for("admin_voice_stream", voice_public_id=voice.public_id)),
        })
    return cards


def get_latest_voice_input(db: Session, order_id: int) -> VoiceInput | None:
    return (
        db.query(VoiceInput)
        .filter(VoiceInput.order_id == order_id)
        .order_by(VoiceInput.id.desc())
        .first()
    )



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


def get_order_payments(order: Order) -> list[OrderPayment]:
    if not order.payments:
        return []
    return sorted(order.payments, key=lambda item: item.id or 0, reverse=True)


def get_payment_by_public_id(order: Order, payment_public_id: str) -> OrderPayment | None:
    for payment in get_order_payments(order):
        if payment.public_id == payment_public_id:
            return payment
    return None


def get_lyrics_versions(db: Session, order_id: int) -> list[LyricsVersion]:
    return (
        db.query(LyricsVersion)
        .filter(LyricsVersion.order_id == order_id)
        .order_by(LyricsVersion.is_selected.desc(), LyricsVersion.id.asc())
        .all()
    )


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


def can_resend_payment_email_for_payment(order: Order, payment: OrderPayment | None) -> bool:
    return bool(payment and payment.status == "succeeded" and order.user and order.user.email)


def can_resend_payment_email(order: Order) -> bool:
    latest_payment = get_latest_payment(order)
    return can_resend_payment_email_for_payment(order, latest_payment)


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
        "amount_rub": latest_payment.final_amount_rub if latest_payment else None,
    }


def build_funnel_counts(db: Session) -> list[dict[str, int | str]]:
    rows = db.query(Order.status, func.count(Order.id)).group_by(Order.status).all()
    counts_map = {status: count for status, count in rows}
    return [
        {"label": label, "count": sum(int(counts_map.get(status, 0) or 0) for status in statuses)}
        for label, statuses in FUNNEL_STAGES
    ]


@router.get("/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    if has_admin_access(request):
        return RedirectResponse(url="/admin/", status_code=303)
    return templates.TemplateResponse("admin/login.html", {"request": request, "page_title": "Вход в админку", "error": None})


@router.post("/login", response_class=HTMLResponse)
async def admin_login_submit(request: Request, token: str = Form(...)):
    if not settings.ADMIN_TOKEN:
        return RedirectResponse(url="/admin/", status_code=303)
    if token.strip() != settings.ADMIN_TOKEN:
        return templates.TemplateResponse(
            "admin/login.html",
            {"request": request, "page_title": "Вход в админку", "error": "Неверный токен."},
            status_code=400,
        )
    request.session["admin_access"] = True
    return RedirectResponse(url="/admin/", status_code=303)


@router.get("/logout")
async def admin_logout(request: Request):
    request.session.pop("admin_access", None)
    return RedirectResponse(url="/", status_code=303)


@router.get("/voice/{voice_public_id}")
async def admin_voice_stream(voice_public_id: str, request: Request, db: Session = Depends(get_db)):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)

    voice_input = db.query(VoiceInput).filter(VoiceInput.public_id == voice_public_id).first()
    if voice_input is None:
        set_admin_flash(request, "error", "Голосовой файл не найден.")
        return RedirectResponse(url="/admin/", status_code=303)

    file_path = Path(voice_input.storage_path)
    if not file_path.exists():
        set_admin_flash(request, "error", "Файл голосового не найден на диске.")
        return RedirectResponse(url=f"/admin/orders/{voice_input.order.public_id}", status_code=303)

    return FileResponse(
        path=file_path,
        media_type=voice_input.content_type,
        filename=voice_input.original_filename or file_path.name,
    )


@router.get("/", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    q: str | None = None,
    order_status: str = FILTER_ALL,
    payment_status: str = FILTER_ALL,
    song_status: str = FILTER_ALL,
    story_source: str = FILTER_ALL,
    lyrics_mode: str = FILTER_ALL,
    db: Session = Depends(get_db),
):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)

    query_text = (q or "").strip()
    order_status = (order_status or FILTER_ALL).strip() or FILTER_ALL
    payment_status = (payment_status or FILTER_ALL).strip() or FILTER_ALL
    song_status = (song_status or FILTER_ALL).strip() or FILTER_ALL
    story_source = (story_source or FILTER_ALL).strip() or FILTER_ALL
    lyrics_mode = (lyrics_mode or FILTER_ALL).strip() or FILTER_ALL

    day_start_utc, day_end_utc = get_today_range_utc()

    new_users_today = db.query(func.count(User.id)).filter(User.created_at >= day_start_utc, User.created_at < day_end_utc).scalar() or 0
    total_users = db.query(func.count(User.id)).scalar() or 0
    orders_today = db.query(func.count(Order.id)).filter(Order.created_at >= day_start_utc, Order.created_at < day_end_utc).scalar() or 0
    successful_payments_today = db.query(func.count(OrderPayment.id)).filter(OrderPayment.status == "succeeded", OrderPayment.paid_at >= day_start_utc, OrderPayment.paid_at < day_end_utc).scalar() or 0
    songs_ready_today = db.query(func.count(SongGeneration.id)).filter(SongGeneration.status == "succeeded", SongGeneration.finished_at >= day_start_utc, SongGeneration.finished_at < day_end_utc).scalar() or 0
    song_errors_today = db.query(func.count(SongGeneration.id)).filter(SongGeneration.status == "failed", SongGeneration.updated_at >= day_start_utc, SongGeneration.updated_at < day_end_utc).scalar() or 0
    pending_payments = db.query(func.count(OrderPayment.id)).filter(OrderPayment.status.in_(["pending", "waiting_for_capture"])).scalar() or 0
    failed_song_jobs = db.query(func.count(SongGeneration.id)).filter(SongGeneration.status == "failed").scalar() or 0

    orders_query = db.query(Order).outerjoin(User, Order.user_id == User.id)
    if query_text:
        pattern = f"%{query_text}%"
        orders_query = orders_query.filter(or_(Order.order_number.ilike(pattern), Order.public_id.ilike(pattern), Order.session_id.ilike(pattern), User.email.ilike(pattern)))
    if order_status != FILTER_ALL:
        orders_query = orders_query.filter(Order.status == order_status)
    if payment_status == "missing":
        orders_query = orders_query.filter(~Order.payments.any())
    elif payment_status != FILTER_ALL:
        orders_query = orders_query.filter(Order.payments.any(OrderPayment.status == payment_status))
    if song_status == "missing":
        orders_query = orders_query.filter(~Order.song_generations.any())
    elif song_status != FILTER_ALL:
        orders_query = orders_query.filter(Order.song_generations.any(SongGeneration.status == song_status))
    if story_source != FILTER_ALL:
        orders_query = orders_query.filter(Order.story_source == story_source)
    if lyrics_mode != FILTER_ALL:
        orders_query = orders_query.filter(Order.lyrics_mode == lyrics_mode)

    orders = orders_query.order_by(Order.id.desc()).limit(50).all()
    order_status_counts = db.query(Order.status, func.count(Order.id)).group_by(Order.status).order_by(func.count(Order.id).desc()).all()
    payment_status_counts = db.query(OrderPayment.status, func.count(OrderPayment.id)).group_by(OrderPayment.status).order_by(func.count(OrderPayment.id).desc()).all()
    song_status_counts = db.query(SongGeneration.status, func.count(SongGeneration.id)).group_by(SongGeneration.status).order_by(func.count(SongGeneration.id).desc()).all()
    recent_failed_songs = db.query(SongGeneration).filter(SongGeneration.status == "failed").order_by(SongGeneration.updated_at.desc(), SongGeneration.id.desc()).limit(10).all()

    recent_problem_payments = db.query(OrderPayment).filter(OrderPayment.status.in_(["pending", "waiting_for_capture", "canceled"])).order_by(OrderPayment.updated_at.desc(), OrderPayment.id.desc()).limit(10).all()

    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "page_title": "Админка",
            "flash": pop_admin_flash(request),
            "q": query_text,
            "order_status": order_status,
            "payment_status": payment_status,
            "song_status": song_status,
            "story_source": story_source,
            "lyrics_mode": lyrics_mode,
            "admin_token_enabled": bool(settings.ADMIN_TOKEN),
            "new_users_today": new_users_today,
            "total_users": total_users,
            "orders_today": orders_today,
            "successful_payments_today": successful_payments_today,
            "songs_ready_today": songs_ready_today,
            "song_errors_today": song_errors_today,
            "failed_song_jobs": failed_song_jobs,
            "pending_payments": pending_payments,
            "order_status_counts": order_status_counts,
            "payment_status_counts": payment_status_counts,
            "song_status_counts": song_status_counts,
            "order_cards": [build_order_card(order) for order in orders],
            "funnel_counts": build_funnel_counts(db),
            "recent_failed_songs": recent_failed_songs,
            "recent_problem_payments": recent_problem_payments,
            "order_status_filter_options": ORDER_STATUS_FILTER_OPTIONS,
            "payment_status_filter_options": PAYMENT_STATUS_FILTER_OPTIONS,
            "song_status_filter_options": SONG_STATUS_FILTER_OPTIONS,
            "story_source_options": STORY_SOURCE_OPTIONS,
            "lyrics_mode_options": LYRICS_MODE_OPTIONS,
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
    payment_attempts = get_order_payments(order)
    pricing_preview = build_order_pricing_preview(db, order)
    latest_song = get_latest_song(order)
    latest_ready_song = get_latest_ready_song(order)
    song_attempts = get_song_attempts(order)
    lyrics_versions = get_lyrics_versions(db, order.id)
    selected_version = next((item for item in lyrics_versions if item.is_selected), None)
    voice_inputs = get_voice_inputs(db, order.id)
    voice_cards = build_voice_cards(request, voice_inputs)
    latest_voice = voice_inputs[0] if voice_inputs else None
    events = db.query(OrderEvent).filter(OrderEvent.order_id == order.id).order_by(OrderEvent.id.desc()).limit(30).all()

    return templates.TemplateResponse(
        "admin/order_detail.html",
        {
            "request": request,
            "page_title": f"Админка · {order.order_number}",
            "flash": pop_admin_flash(request),
            "admin_token_enabled": bool(settings.ADMIN_TOKEN),
            "order": order,
            "latest_payment": latest_payment,
            "payment_attempts": payment_attempts,
            "pricing_preview": pricing_preview,
            "latest_song": latest_song,
            "latest_ready_song": latest_ready_song,
            "song_attempts": song_attempts,
            "has_previous_ready_song": bool(latest_ready_song and latest_song and latest_ready_song.public_id != latest_song.public_id),
            "lyrics_versions": lyrics_versions,
            "selected_version": selected_version,
            "voice_cards": voice_cards,
            "latest_voice": latest_voice,
            "voice_source_is_active": order.story_source == "voice",
            "payment_status_label": humanize_payment_status(latest_payment.status if latest_payment else None),
            "song_status_label": humanize_song_status(latest_song.status if latest_song else None),
            "can_run_song": can_run_song(order),
            "can_resend_payment_email": can_resend_payment_email(order),
            "can_resend_song_ready_email": can_resend_song_ready_email(order),
            "order_status_options": ORDER_STATUS_OPTIONS,
            "song_status_options": SONG_STATUS_OPTIONS,
            "events": events,
            "humanize_payment_status": humanize_payment_status,
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
    if latest_payment is None:
        set_admin_flash(request, "warning", "У заказа нет платежа для синхронизации.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    return await admin_order_payment_sync_attempt(order_public_id, latest_payment.public_id, request, db)


@router.post("/orders/{order_public_id}/payments/{payment_public_id}/sync")
async def admin_order_payment_sync_attempt(order_public_id: str, payment_public_id: str, request: Request, db: Session = Depends(get_db)):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    order = db.query(Order).filter(Order.public_id == order_public_id).first()
    if order is None:
        set_admin_flash(request, "error", "Заказ не найден.")
        return RedirectResponse(url="/admin/", status_code=303)

    payment = get_payment_by_public_id(order, payment_public_id)
    if payment is None:
        set_admin_flash(request, "warning", "Платёж не найден у этого заказа.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)
    if not payment.yookassa_payment_id:
        set_admin_flash(request, "warning", "У этого платежа нет внешнего payment id для синхронизации.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    try:
        sync_payment_with_remote(db, payment, trigger="admin_manual_sync", event_name="admin_payment_status_synced")
        db.commit()
    except YooKassaError as exc:
        db.rollback()
        set_admin_flash(request, "error", str(exc))
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    set_admin_flash(request, "success", f"Статус платежа {payment.public_id} обновлён: {payment.status}.")
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
async def admin_order_status_update(order_public_id: str, request: Request, status: str = Form(...), db: Session = Depends(get_db)):
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
    db.add(OrderEvent(order=order, event_type="admin_order_status_changed", payload={"status_from": previous_status, "status_to": target_status, "trigger": "admin_manual_status_change"}))
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
        song.error_message = manual_error or None
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
    else:
        if song.started_at is None:
            song.started_at = utcnow()
        song.finished_at = utcnow()
        order.status = "paid"
        song.error_message = manual_error or song.error_message

    if urls:
        song.audio_url = urls[0]
        song.result_tracks = build_manual_result_tracks(urls)
    elif target_status == "succeeded" and song.audio_url and not song.result_tracks:
        song.result_tracks = build_manual_result_tracks([song.audio_url])

    db.add(OrderEvent(order=order, event_type="admin_song_status_changed", payload={
        "song_job_id": song.public_id,
        "status_from": previous_status,
        "status_to": target_status,
        "order_status_from": previous_order_status,
        "order_status_to": order.status,
        "audio_url_count": len(urls),
        "has_error_message": bool(song.error_message),
        "trigger": "admin_manual_status_change",
    }))
    db.commit()
    set_admin_flash(request, "success", f"Статус песни изменён: {previous_status} → {target_status}.")
    return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)


@router.post("/orders/{order_public_id}/final-lyrics-update")
async def admin_order_final_lyrics_update(order_public_id: str, request: Request, final_lyrics_text: str = Form(""), db: Session = Depends(get_db)):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    order = db.query(Order).filter(Order.public_id == order_public_id).first()
    if order is None:
        set_admin_flash(request, "error", "Заказ не найден.")
        return RedirectResponse(url="/admin/", status_code=303)

    value = (final_lyrics_text or "").strip()
    if not value:
        set_admin_flash(request, "warning", "Финальный текст не может быть пустым.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    previous_text = (order.final_lyrics_text or "").strip()
    if previous_text == value:
        set_admin_flash(request, "warning", "Финальный текст не изменился.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    order.final_lyrics_text = value
    selected_version = next((item for item in get_lyrics_versions(db, order.id) if item.is_selected), None)
    if selected_version is not None:
        selected_version.edited_lyrics_text = value

    db.add(OrderEvent(order=order, event_type="admin_final_lyrics_updated", payload={
        "selected_version_id": selected_version.public_id if selected_version else None,
        "text_length": len(value),
        "trigger": "admin_manual_lyrics_update",
    }))
    db.commit()
    set_admin_flash(request, "success", "Финальный текст сохранён.")
    return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)


@router.post("/orders/{order_public_id}/lyrics-select")
async def admin_order_lyrics_select(order_public_id: str, request: Request, version_public_id: str = Form(...), db: Session = Depends(get_db)):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    order = db.query(Order).filter(Order.public_id == order_public_id).first()
    if order is None:
        set_admin_flash(request, "error", "Заказ не найден.")
        return RedirectResponse(url="/admin/", status_code=303)

    versions = get_lyrics_versions(db, order.id)
    selected_version = next((item for item in versions if item.public_id == version_public_id), None)
    if selected_version is None:
        set_admin_flash(request, "error", "Версия текста не найдена.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    for version in versions:
        version.is_selected = version.public_id == selected_version.public_id

    final_text = (selected_version.edited_lyrics_text or selected_version.lyrics_text or "").strip()
    if final_text:
        order.final_lyrics_text = final_text

    db.add(OrderEvent(order=order, event_type="admin_lyrics_version_selected", payload={
        "version_id": selected_version.public_id,
        "variant": selected_version.angle_label,
        "provider": selected_version.provider,
        "has_edited_text": bool(selected_version.edited_lyrics_text),
        "trigger": "admin_version_pick",
    }))
    db.commit()
    set_admin_flash(request, "success", f"Выбрана версия текста: {selected_version.angle_label}.")
    return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)


@router.post("/orders/{order_public_id}/transcript-update")
async def admin_order_transcript_update(
    order_public_id: str,
    request: Request,
    transcript_text: str = Form(""),
    sync_latest_voice: bool = Form(False),
    db: Session = Depends(get_db),
):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    order = db.query(Order).filter(Order.public_id == order_public_id).first()
    if order is None:
        set_admin_flash(request, "error", "Заказ не найден.")
        return RedirectResponse(url="/admin/", status_code=303)

    value = (transcript_text or "").strip()
    if not value:
        set_admin_flash(request, "warning", "Расшифровка не может быть пустой.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    previous_text = (order.transcript_text or "").strip()
    if previous_text == value:
        set_admin_flash(request, "warning", "Расшифровка не изменилась.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    order.transcript_text = value
    synced_voice_id = None
    latest_voice = get_latest_voice_input(db, order.id)
    if sync_latest_voice and latest_voice is not None:
        latest_voice.transcript_text = value
        latest_voice.transcription_status = "done"
        synced_voice_id = latest_voice.public_id

    db.add(OrderEvent(order=order, event_type="admin_transcript_updated", payload={
        "text_length": len(value),
        "synced_voice_id": synced_voice_id,
        "trigger": "admin_manual_transcript_update",
    }))
    db.commit()
    set_admin_flash(request, "success", "Расшифровка сохранена.")
    return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)


@router.post("/orders/{order_public_id}/voice-apply")
async def admin_order_voice_apply(
    order_public_id: str,
    request: Request,
    voice_public_id: str = Form(...),
    db: Session = Depends(get_db),
):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    order = db.query(Order).filter(Order.public_id == order_public_id).first()
    if order is None:
        set_admin_flash(request, "error", "Заказ не найден.")
        return RedirectResponse(url="/admin/", status_code=303)

    voice_input = (
        db.query(VoiceInput)
        .filter(VoiceInput.order_id == order.id, VoiceInput.public_id == voice_public_id)
        .first()
    )
    if voice_input is None:
        set_admin_flash(request, "error", "Голосовое не найдено.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    transcript = (voice_input.transcript_text or "").strip()
    if not transcript:
        set_admin_flash(request, "warning", "У выбранного голосового пока нет расшифровки.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    order.transcript_text = transcript
    db.add(OrderEvent(order=order, event_type="admin_voice_transcript_applied", payload={
        "voice_input_id": voice_input.public_id,
        "text_length": len(transcript),
        "trigger": "admin_voice_apply",
    }))
    db.commit()
    set_admin_flash(request, "success", "Расшифровка из голосового подставлена в заказ.")
    return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)


@router.post("/orders/{order_public_id}/voice-retranscribe")
async def admin_order_voice_retranscribe(
    order_public_id: str,
    request: Request,
    voice_public_id: str = Form(...),
    apply_to_order: bool = Form(False),
    db: Session = Depends(get_db),
):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    order = db.query(Order).filter(Order.public_id == order_public_id).first()
    if order is None:
        set_admin_flash(request, "error", "Заказ не найден.")
        return RedirectResponse(url="/admin/", status_code=303)

    voice_input = (
        db.query(VoiceInput)
        .filter(VoiceInput.order_id == order.id, VoiceInput.public_id == voice_public_id)
        .first()
    )
    if voice_input is None:
        set_admin_flash(request, "error", "Голосовое не найдено.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    file_path = Path(voice_input.storage_path)
    if not file_path.exists():
        set_admin_flash(request, "error", "Файл голосового не найден на сервере.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    voice_input.transcription_status = "transcribing"
    db.add(OrderEvent(order=order, event_type="admin_voice_retranscription_started", payload={
        "voice_input_id": voice_input.public_id,
        "trigger": "admin_voice_retranscribe",
    }))
    db.commit()

    try:
        result = await transcribe_audio_file(voice_input.storage_path)
    except TranscriptionServiceError as exc:
        voice_input.transcription_status = "failed"
        db.add(OrderEvent(order=order, event_type="admin_voice_retranscription_failed", payload={
            "voice_input_id": voice_input.public_id,
            "error": str(exc),
            "trigger": "admin_voice_retranscribe",
        }))
        db.commit()
        set_admin_flash(request, "error", str(exc))
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    voice_input.transcription_status = "done"
    voice_input.transcript_text = result.text
    if apply_to_order:
        order.transcript_text = result.text

    db.add(OrderEvent(order=order, event_type="admin_voice_retranscription_done", payload={
        "voice_input_id": voice_input.public_id,
        "text_length": len(result.text),
        "model": result.model,
        "language": result.language,
        "applied_to_order": bool(apply_to_order),
        "trigger": "admin_voice_retranscribe",
    }))
    db.commit()
    set_admin_flash(request, "success", "Голосовое заново расшифровано.")
    return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)


@router.post("/orders/{order_public_id}/lyrics-regenerate")
async def admin_order_lyrics_regenerate(order_public_id: str, request: Request, db: Session = Depends(get_db)):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    order = db.query(Order).filter(Order.public_id == order_public_id).first()
    if order is None:
        set_admin_flash(request, "error", "Заказ не найден.")
        return RedirectResponse(url="/admin/", status_code=303)

    if order.lyrics_mode != "generate":
        set_admin_flash(request, "warning", "Перегенерация текстов доступна только для режима генерации.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    source_text = (order.transcript_text if order.story_source == "voice" else order.story_text or "").strip()
    if not source_text:
        set_admin_flash(request, "warning", "Сначала нужен исходный текст для генерации.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    db.add(OrderEvent(order=order, event_type="admin_lyrics_regeneration_started", payload={
        "story_source": order.story_source,
        "text_length": len(source_text),
        "trigger": "admin_manual_regenerate",
    }))
    db.commit()

    try:
        result: DualGenerationResult = await generate_dual_lyrics_versions(source_text)
    except LyricsGenerationError as exc:
        db.add(OrderEvent(order=order, event_type="admin_lyrics_regeneration_failed", payload={
            "error": str(exc),
            "trigger": "admin_manual_regenerate",
        }))
        db.commit()
        set_admin_flash(request, "error", str(exc))
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

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

    variant_errors = [{
        "slot_label": err.slot_label,
        "user_message": err.user_message,
        "technical_message": err.technical_message,
    } for err in result.errors]

    db.add(OrderEvent(order=order, event_type="admin_lyrics_regeneration_done", payload={
        "versions_count": len(result.versions),
        "selected_version_id": selected_version_id,
        "model": result.versions[0].model_name if result.versions else None,
        "errors": variant_errors,
        "trigger": "admin_manual_regenerate",
    }))
    db.commit()
    set_admin_flash(request, "success", f"Тексты перегенерированы. Получено версий: {len(result.versions)}.")
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
    if latest_payment is None:
        set_admin_flash(request, "warning", "У заказа нет платежа для письма.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    return await admin_order_payment_email_resend_attempt(order_public_id, latest_payment.public_id, request, db)


@router.post("/orders/{order_public_id}/payments/{payment_public_id}/payment-email-resend")
async def admin_order_payment_email_resend_attempt(order_public_id: str, payment_public_id: str, request: Request, db: Session = Depends(get_db)):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    order = db.query(Order).filter(Order.public_id == order_public_id).first()
    if order is None:
        set_admin_flash(request, "error", "Заказ не найден.")
        return RedirectResponse(url="/admin/", status_code=303)

    payment = get_payment_by_public_id(order, payment_public_id)
    if payment is None or payment.status != "succeeded":
        set_admin_flash(request, "warning", "Письмо об оплате можно отправить только для успешно оплаченного платежа.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    try:
        resend_payment_success_email(db, order, payment)
        db.commit()
    except EmailServiceError as exc:
        db.rollback()
        set_admin_flash(request, "error", str(exc))
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    set_admin_flash(request, "success", f"Письмо об успешной оплате отправлено повторно для платежа {payment.public_id}.")
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
