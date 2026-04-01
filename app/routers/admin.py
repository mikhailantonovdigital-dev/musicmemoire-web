from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from sqlalchemy import distinct, func, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.security import utcnow
from app.core.storage import StorageError, ensure_support_attachment_local_path, ensure_voice_input_local_path, object_storage_enabled
from app.core.templates import templates
from app.models import BackgroundJob, EmailLog, LyricsVersion, Order, OrderEvent, OrderPayment, SecurityEvent, SongGeneration, SupportMessage, SupportThread, User, VoiceInput
from app.models.order_payment import build_order_pricing_preview, payment_success_at_expr
from app.services.payment_workflow import resend_payment_success_email, sync_payment_with_remote
from app.services.song_workflow import (
    RUNNING_SONG_STATUSES,
    create_song_job_record,
    get_latest_ready_song,
    get_latest_song,
    get_song_attempts,
    has_successful_payment,
    humanize_song_status,
    maybe_send_song_failed_email,
    resend_song_failed_email,
    resend_song_ready_email,
    sync_song_job_state,
)
from app.services.suno_service import SunoServiceError
from app.services.background_jobs import BackgroundJobError, get_job_label, enqueue_background_job, find_active_job_for_order
from app.services.email_log_service import humanize_email_status, humanize_email_type
from app.tasks import run_admin_lyrics_regeneration_task, run_song_start_task, run_voice_transcription_task
from app.services.yookassa_service import YooKassaError
from app.services.telegram_report_service import build_test_report, notify_admin_support_reply, send_telegram_report, telegram_reporting_enabled

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
SUPPORT_STATUS_OPTIONS = [
    (FILTER_ALL, "Любой статус обращения"),
    ("new", "new"),
    ("open", "open"),
    ("pending", "pending"),
    ("closed", "closed"),
]
VALID_ORDER_STATUSES = {value for value, _label in ORDER_STATUS_OPTIONS}
VALID_SONG_STATUSES = {value for value, _label in SONG_STATUS_OPTIONS}
VALID_SUPPORT_STATUSES = {value for value, _label in SUPPORT_STATUS_OPTIONS if value != FILTER_ALL}
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
        "queued": "В очереди",
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
        storage_backend = (voice.storage_backend or "local").strip() or "local"
        cards.append({
            "voice": voice,
            "size_label": format_size(voice.size_bytes),
            "status_label": humanize_transcription_status(voice.transcription_status),
            "storage_label": "object storage" if storage_backend == "s3" else "локальный диск",
            "storage_key": voice.storage_key,
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
        request.session["admin_access"] = True
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


def build_dashboard_metrics(
    db: Session,
    *,
    day_start_utc: datetime | None = None,
    day_end_utc: datetime | None = None,
) -> list[dict[str, str | int]]:
    session_query = db.query(Order)
    starts_query = db.query(Order)
    final_step_query = db.query(Order).filter(Order.user_id.isnot(None))

    payment_success_at = payment_success_at_expr()
    paid_query = db.query(OrderPayment).filter(OrderPayment.status == "succeeded")

    if day_start_utc and day_end_utc:
        session_query = session_query.filter(Order.created_at >= day_start_utc, Order.created_at < day_end_utc)
        starts_query = starts_query.filter(Order.created_at >= day_start_utc, Order.created_at < day_end_utc)
        final_step_query = final_step_query.filter(Order.updated_at >= day_start_utc, Order.updated_at < day_end_utc)
        paid_query = paid_query.filter(payment_success_at >= day_start_utc, payment_success_at < day_end_utc)

    visitor_count = session_query.with_entities(func.count(distinct(Order.session_id))).scalar() or 0
    starts_count = starts_query.with_entities(func.count(Order.id)).scalar() or 0
    final_step_count = final_step_query.with_entities(func.count(Order.id)).scalar() or 0

    paid_items = paid_query.all()
    paid_count = len(paid_items)
    paid_sum = sum(item.final_amount_rub for item in paid_items)
    unique_clients = len({item.user_id for item in paid_items if item.user_id})

    return [
        {"label": "Посетители сайта", "value": int(visitor_count)},
        {"label": "Нажали кнопку «Хочу песню» (старт анкеты)", "value": int(starts_count)},
        {"label": "Дошли до финального шага (ввод email и оплата)", "value": int(final_step_count)},
        {"label": "Оплатили", "value": int(paid_count)},
        {"label": "Сумма оплат", "value": f"{int(paid_sum)} ₽"},
        {"label": "Уникальных клиентов", "value": int(unique_clients)},
    ]


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


def humanize_order_status(status: str | None) -> str:
    mapping = {
        "draft": "Черновик",
        "awaiting_payment": "Ожидает оплату",
        "payment_pending": "Оплата в процессе",
        "payment_canceled": "Оплата отменена",
        "paid": "Оплачен",
        "song_pending": "Песня в работе",
        "song_ready": "Песня готова",
        "song_failed": "Ошибка генерации",
    }
    return mapping.get(status or "", status or "—")


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


def can_resend_song_failed_email(order: Order) -> bool:
    latest_song = get_latest_song(order)
    return bool(latest_song and latest_song.status == "failed" and order.user and order.user.email)


def humanize_security_action(action: str | None) -> str:
    mapping = {
        "account_magic_link_send": "Ссылка для входа в кабинет",
        "questionnaire_magic_link_send": "Ссылка после анкеты",
        "questionnaire_voice_upload": "Загрузка голосового",
        "questionnaire_voice_retranscribe": "Перезапуск расшифровки",
        "song_generation_start": "Запуск генерации песни",
    }
    return mapping.get(action or "", action or "—")


def humanize_security_status(status: str | None) -> str:
    mapping = {
        "allowed": "Разрешено",
        "blocked": "Заблокировано",
        "suspicious": "Подозрительно",
    }
    return mapping.get(status or "", status or "—")






def humanize_support_status(status: str | None) -> str:
    mapping = {
        "new": "Новое",
        "open": "В работе",
        "pending": "Ждёт клиента",
        "closed": "Закрыто",
    }
    return mapping.get(status or "", status or "—")


def get_support_threads_for_order(db: Session, order_id: int) -> list[SupportThread]:
    return (
        db.query(SupportThread)
        .filter(SupportThread.order_id == order_id)
        .order_by(SupportThread.updated_at.desc(), SupportThread.id.desc())
        .all()
    )


def get_recent_support_threads(db: Session, limit: int = 20) -> list[SupportThread]:
    return db.query(SupportThread).order_by(SupportThread.updated_at.desc(), SupportThread.id.desc()).limit(limit).all()


def get_support_thread_by_public_id(db: Session, thread_public_id: str | None) -> SupportThread | None:
    if not thread_public_id:
        return None
    return db.query(SupportThread).filter(SupportThread.public_id == thread_public_id).first()


def build_support_thread_card(thread: SupportThread) -> dict:
    visible_messages = [item for item in thread.messages if not item.is_internal]
    last_message = thread.messages[-1] if thread.messages else None
    return {
        "thread": thread,
        "status_label": humanize_support_status(thread.status),
        "message_count": len(thread.messages),
        "visible_message_count": len(visible_messages),
        "last_message": last_message,
        "order_number": thread.order.order_number if thread.order else "—",
        "email": thread.email or (thread.user.email if thread.user and thread.user.email else "—"),
    }

def humanize_background_job_status(status: str | None) -> str:
    mapping = {
        "queued": "В очереди",
        "started": "В работе",
        "succeeded": "Успешно",
        "failed": "Ошибка",
    }
    return mapping.get(status or "", status or "—")


def build_background_job_card(job: BackgroundJob) -> dict:
    payload = job.payload if isinstance(job.payload, dict) else {}
    return {
        "job": job,
        "job_label": get_job_label(job.job_type),
        "status_label": humanize_background_job_status(job.status),
        "song_job_id": payload.get("song_public_id") or payload.get("song_job_id"),
        "voice_input_id": payload.get("voice_public_id") or payload.get("voice_input_id"),
        "payment_id": payload.get("payment_public_id"),
    }


def get_recent_background_jobs(db: Session, limit: int = 20) -> list[BackgroundJob]:
    return db.query(BackgroundJob).order_by(BackgroundJob.id.desc()).limit(limit).all()


def get_recent_background_jobs_for_order(db: Session, order_id: int, limit: int = 20) -> list[BackgroundJob]:
    return (
        db.query(BackgroundJob)
        .filter(BackgroundJob.order_id == order_id)
        .order_by(BackgroundJob.id.desc())
        .limit(limit)
        .all()
    )

def build_email_log_card(log: EmailLog) -> dict:
    payload = log.payload if isinstance(log.payload, dict) else {}
    return {
        "log": log,
        "type_label": humanize_email_type(log.email_type),
        "status_label": humanize_email_status(log.status),
        "order_number": log.order.order_number if log.order else "—",
        "context_id": payload.get("payment_public_id") or payload.get("song_job_id") or payload.get("source") or "—",
    }


def get_recent_email_logs(db: Session, limit: int = 20) -> list[EmailLog]:
    return db.query(EmailLog).order_by(EmailLog.id.desc()).limit(limit).all()


def get_recent_email_logs_for_order(db: Session, order_id: int, limit: int = 20) -> list[EmailLog]:
    return (
        db.query(EmailLog)
        .filter(EmailLog.order_id == order_id)
        .order_by(EmailLog.id.desc())
        .limit(limit)
        .all()
    )


def build_security_event_card(event: SecurityEvent) -> dict:
    payload = event.payload if isinstance(event.payload, dict) else {}
    return {
        "event": event,
        "action_label": humanize_security_action(event.action),
        "status_label": humanize_security_status(event.status),
        "order_number": event.order.order_number if event.order else "—",
        "scope_label": f"{event.scope_kind}: {event.scope_value}",
        "ip": payload.get("ip") or "—",
        "path": payload.get("path") or "—",
        "recent_count": payload.get("recent_count"),
        "limit": payload.get("limit"),
        "window_seconds": payload.get("window_seconds"),
    }


def get_recent_security_events_for_order(db: Session, order_id: int, limit: int = 20) -> list[SecurityEvent]:
    return (
        db.query(SecurityEvent)
        .filter(SecurityEvent.order_id == order_id)
        .order_by(SecurityEvent.id.desc())
        .limit(limit)
        .all()
    )


def build_order_card(order: Order) -> dict:
    latest_payment = get_latest_payment(order)
    return {
        "order": order,
        "email": order.user.email if order.user and order.user.email else "—",
        "order_status_label": humanize_order_status(order.status),
        "payment_status_label": humanize_payment_status(latest_payment.status if latest_payment else None),
    }


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

    try:
        file_path = ensure_voice_input_local_path(voice_input)
    except StorageError:
        set_admin_flash(request, "error", "Файл голосового не найден на сервере.")
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
    db: Session = Depends(get_db),
):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)

    query_text = (q or "").strip()

    day_start_utc, day_end_utc = get_today_range_utc()
    today_metrics = build_dashboard_metrics(db, day_start_utc=day_start_utc, day_end_utc=day_end_utc)
    all_time_metrics = build_dashboard_metrics(db)

    orders_query = db.query(Order).outerjoin(User, Order.user_id == User.id)
    if query_text:
        pattern = f"%{query_text}%"
        orders_query = orders_query.filter(or_(Order.order_number.ilike(pattern), User.email.ilike(pattern)))

    orders = orders_query.order_by(Order.id.desc()).limit(50).all()
    return templates.TemplateResponse(
        "admin/dashboard.html",
        {
            "request": request,
            "page_title": "Админка",
            "flash": pop_admin_flash(request),
            "q": query_text,
            "admin_token_enabled": bool(settings.ADMIN_TOKEN),
            "today_metrics": today_metrics,
            "all_time_metrics": all_time_metrics,
            "order_cards": [build_order_card(order) for order in orders],
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
    security_events = get_recent_security_events_for_order(db, order.id, limit=20)
    background_jobs = get_recent_background_jobs_for_order(db, order.id, limit=20)
    support_threads = get_support_threads_for_order(db, order.id)
    email_logs = get_recent_email_logs_for_order(db, order.id, limit=30)

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
            "can_resend_song_failed_email": can_resend_song_failed_email(order),
            "order_status_options": ORDER_STATUS_OPTIONS,
            "song_status_options": SONG_STATUS_OPTIONS,
            "events": events,
            "security_events": [build_security_event_card(item) for item in security_events],
            "background_jobs": [build_background_job_card(item) for item in background_jobs],
            "support_threads": [build_support_thread_card(item) for item in support_threads],
            "email_logs": [build_email_log_card(item) for item in email_logs],
            "humanize_email_type": humanize_email_type,
            "humanize_email_status": humanize_email_status,
            "humanize_payment_status": humanize_payment_status,
            "humanize_security_action": humanize_security_action,
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

    active_background_job = find_active_job_for_order(db, order, "song_generation_start")
    if active_background_job is not None:
        active_song = next((item for item in order.song_generations if item.status in RUNNING_SONG_STATUSES), None)
        if active_song is not None:
            set_admin_flash(request, "info", f"По заказу уже есть активная генерация песни. Открыта попытка #{active_song.attempt_no}.")
            return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    try:
        song = create_song_job_record(db, order, queued_event_type="admin_song_generation_enqueued", trigger="admin_manual_start")
        background_job = enqueue_background_job(
            db,
            order=order,
            job_type="song_generation_start",
            func=run_song_start_task,
            payload={
                "song_public_id": song.public_id,
                "order_public_id": order.public_id,
                "started_event_type": "admin_song_generation_started",
                "failed_event_type": "admin_song_generation_failed",
                "trigger": "admin_manual_start",
            },
        )
        db.commit()
        db.refresh(song)
    except (SunoServiceError, BackgroundJobError) as exc:
        db.rollback()
        set_admin_flash(request, "error", str(exc))
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    set_admin_flash(request, "success", f"Генерация песни поставлена в очередь. Попытка #{song.attempt_no}, job {background_job.public_id}.")
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
        maybe_send_song_failed_email(db, song)
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

    try:
        ensure_voice_input_local_path(voice_input)
    except StorageError:
        set_admin_flash(request, "error", "Файл голосового не найден на сервере.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    voice_input.transcription_status = "queued"
    db.add(voice_input)
    force_sync_transcription = not object_storage_enabled() and not settings.BACKGROUND_JOBS_SYNC_MODE
    try:
        background_job = enqueue_background_job(
            db,
            order=order,
            job_type="voice_transcription",
            func=run_voice_transcription_task,
            payload={
                "order_public_id": order.public_id,
                "voice_public_id": voice_input.public_id,
                "apply_to_order": apply_to_order,
                "started_event_type": "admin_voice_retranscription_started",
                "success_event_type": "admin_voice_retranscription_done",
                "failure_event_type": "admin_voice_retranscription_failed",
                "trigger": "admin_voice_retranscribe",
            },
            force_sync=force_sync_transcription,
        )
        db.commit()
    except BackgroundJobError as exc:
        db.rollback()
        set_admin_flash(request, "error", str(exc))
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    if force_sync_transcription:
        set_admin_flash(request, "success", "Перерасшифровка выполнена сразу, без очереди, потому что object storage ещё не настроен.")
    else:
        set_admin_flash(request, "success", f"Перерасшифровка поставлена в очередь. Job {background_job.public_id}.")
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

    try:
        background_job = enqueue_background_job(
            db,
            order=order,
            job_type="lyrics_regeneration",
            func=run_admin_lyrics_regeneration_task,
            payload={
                "order_public_id": order.public_id,
            },
        )
        db.commit()
    except BackgroundJobError as exc:
        db.rollback()
        set_admin_flash(request, "error", str(exc))
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    set_admin_flash(request, "success", f"Перегенерация текстов поставлена в очередь. Job {background_job.public_id}.")
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
    except RuntimeError as exc:
        db.rollback()
        set_admin_flash(request, "error", str(exc))
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    set_admin_flash(request, "success", f"Переотправка письма об оплате поставлена в очередь для платежа {payment.public_id}.")
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
    except RuntimeError as exc:
        db.rollback()
        set_admin_flash(request, "error", str(exc))
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    set_admin_flash(request, "success", "Переотправка письма о готовой песне поставлена в очередь.")
    return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)


@router.post("/orders/{order_public_id}/song-failed-email-resend")
async def admin_order_song_failed_email_resend(order_public_id: str, request: Request, db: Session = Depends(get_db)):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    order = db.query(Order).filter(Order.public_id == order_public_id).first()
    if order is None:
        set_admin_flash(request, "error", "Заказ не найден.")
        return RedirectResponse(url="/admin/", status_code=303)

    latest_song = get_latest_song(order)
    if latest_song is None or latest_song.status != "failed":
        set_admin_flash(request, "warning", "Письмо об ошибке можно отправить только для заказа с ошибкой песни.")
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    try:
        resend_song_failed_email(db, latest_song)
        db.commit()
    except RuntimeError as exc:
        db.rollback()
        set_admin_flash(request, "error", str(exc))
        return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)

    set_admin_flash(request, "success", "Переотправка письма об ошибке поставлена в очередь.")
    return RedirectResponse(url=f"/admin/orders/{order.public_id}", status_code=303)


@router.get("/support", response_class=HTMLResponse)
async def admin_support_dashboard(
    request: Request,
    q: str | None = None,
    status: str = FILTER_ALL,
    thread: str | None = None,
    db: Session = Depends(get_db),
):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)

    query_text = (q or "").strip()
    status = (status or FILTER_ALL).strip() or FILTER_ALL

    threads_query = db.query(SupportThread).outerjoin(Order, SupportThread.order_id == Order.id).outerjoin(User, SupportThread.user_id == User.id)
    if query_text:
        pattern = f"%{query_text}%"
        threads_query = threads_query.filter(
            or_(
                SupportThread.public_id.ilike(pattern),
                SupportThread.email.ilike(pattern),
                SupportThread.subject.ilike(pattern),
                Order.order_number.ilike(pattern),
                Order.public_id.ilike(pattern),
                User.email.ilike(pattern),
            )
        )
    if status != FILTER_ALL:
        threads_query = threads_query.filter(SupportThread.status == status)

    threads = threads_query.order_by(SupportThread.updated_at.desc(), SupportThread.id.desc()).limit(100).all()
    selected_thread = get_support_thread_by_public_id(db, thread) or (threads[0] if threads else None)

    return templates.TemplateResponse(
        "admin/support.html",
        {
            "request": request,
            "page_title": "Поддержка — админка",
            "flash": pop_admin_flash(request),
            "admin_token_enabled": bool(settings.ADMIN_TOKEN),
            "q": query_text,
            "status": status,
            "status_options": SUPPORT_STATUS_OPTIONS,
            "thread_cards": [build_support_thread_card(item) for item in threads],
            "selected_thread": build_support_thread_card(selected_thread) if selected_thread else None,
            "telegram_reporting_enabled": telegram_reporting_enabled(),
        },
    )


@router.post("/support/{thread_public_id}/status")
async def admin_support_status_update(
    thread_public_id: str,
    request: Request,
    status: str = Form(...),
    db: Session = Depends(get_db),
):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    thread = get_support_thread_by_public_id(db, thread_public_id)
    if thread is None:
        set_admin_flash(request, "error", "Обращение не найдено.")
        return RedirectResponse(url="/admin/support", status_code=303)
    target_status = (status or "").strip()
    if target_status not in VALID_SUPPORT_STATUSES:
        set_admin_flash(request, "error", "Недопустимый статус обращения.")
        return RedirectResponse(url=f"/admin/support?thread={thread.public_id}", status_code=303)
    previous_status = thread.status
    thread.status = target_status
    if thread.order is not None:
        db.add(OrderEvent(order=thread.order, event_type="support_thread_status_changed", payload={"thread_public_id": thread.public_id, "status_from": previous_status, "status_to": target_status}))
    db.commit()
    set_admin_flash(request, "success", f"Статус обращения изменён: {previous_status} → {target_status}.")
    return RedirectResponse(url=f"/admin/support?thread={thread.public_id}", status_code=303)


@router.post("/support/{thread_public_id}/reply")
async def admin_support_reply(
    thread_public_id: str,
    request: Request,
    body: str = Form(""),
    is_internal: str | None = Form(None),
    db: Session = Depends(get_db),
):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    thread = get_support_thread_by_public_id(db, thread_public_id)
    if thread is None:
        set_admin_flash(request, "error", "Обращение не найдено.")
        return RedirectResponse(url="/admin/support", status_code=303)
    message_text = (body or "").strip()
    if not message_text:
        set_admin_flash(request, "error", "Введите текст ответа или заметки.")
        return RedirectResponse(url=f"/admin/support?thread={thread.public_id}", status_code=303)

    message = SupportMessage(sender_role="admin", body=message_text, is_internal=bool(is_internal))
    thread.messages.append(message)
    if thread.status == "new":
        thread.status = "open"
    if thread.order is not None:
        db.add(OrderEvent(order=thread.order, event_type="support_thread_admin_reply_added", payload={"thread_public_id": thread.public_id, "is_internal": bool(is_internal)}))
    db.commit()
    notify_admin_support_reply(thread, message)
    set_admin_flash(request, "success", "Сообщение в обращение добавлено.")
    return RedirectResponse(url=f"/admin/support?thread={thread.public_id}", status_code=303)


@router.post("/support/telegram-report/test")
async def admin_support_telegram_report_test(request: Request):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    result = send_telegram_report(build_test_report())
    set_admin_flash(request, "success" if result.ok else "error", result.detail)
    return RedirectResponse(url="/admin/support", status_code=303)


@router.get("/support/attachments/{message_id}")
async def admin_support_attachment_download(message_id: int, request: Request, db: Session = Depends(get_db)):
    if not has_admin_access(request):
        return RedirectResponse(url="/admin/login", status_code=303)
    message = db.query(SupportMessage).filter(SupportMessage.id == message_id).first()
    if message is None or not message.attachment_relative_path:
        return RedirectResponse(url="/admin/support", status_code=303)
    try:
        local_path = ensure_support_attachment_local_path(message)
    except StorageError as exc:
        set_admin_flash(request, "error", str(exc))
        return RedirectResponse(url="/admin/support", status_code=303)
    return FileResponse(path=local_path, filename=message.attachment_original_filename or local_path.name, media_type=message.attachment_content_type or "application/octet-stream")
