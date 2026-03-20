from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.security import (
    generate_magic_token,
    get_session_user,
    hash_magic_token,
    is_valid_email,
    normalize_email,
    utcnow,
)
from app.core.templates import templates
from app.models import MagicLoginToken, Order, User
from app.models.order_payment import build_order_pricing_preview
from app.services.email_log_service import create_email_log
from app.services.email_service import EmailServiceError, magic_link_email_subject, send_magic_link_email
from app.services.payment_workflow import FINAL_PAYMENT_STATUSES, sync_payment_with_remote
from app.services.rate_limit_service import RateLimitRule, enforce_rate_limit, get_client_ip
from app.services.song_workflow import get_latest_ready_song, get_latest_song, get_song_attempts, sync_song_job_state
from app.services.suno_service import SunoServiceError

router = APIRouter(prefix="/account", tags=["account"])

RUNNING_SONG_STATUSES = {"queued", "processing"}


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


def humanize_song_style(style: str | None) -> str:
    mapping = {
        "pop": "Поп",
        "rap": "Рэп",
        "rock": "Рок",
        "chanson": "Шансон",
        "indie": "Инди",
        "multi": "Несколько стилей",
        "custom": "Свой вариант",
    }
    return mapping.get((style or "").strip().lower(), "Не выбран")


def humanize_singer_gender(gender: str | None) -> str:
    mapping = {
        "male": "Мужской голос",
        "female": "Женский голос",
    }
    return mapping.get((gender or "").strip().lower(), "Не выбран")


def humanize_song_mood(mood: str | None) -> str:
    mapping = {
        "romantic": "Романтичное",
        "uplifting": "Воодушевляющее",
        "nostalgic": "Ностальгичное",
        "dramatic": "Драматичное",
        "party": "Праздничное",
    }
    return mapping.get((mood or "").strip().lower(), "Не выбрано")


def build_song_profile(order: Order) -> dict[str, str | None]:
    style_code = (order.song_style or "").strip().lower()
    style_custom = (order.song_style_custom or "").strip()

    style_details = None
    if style_code in {"multi", "custom"}:
        style_details = style_custom or "Не указано"

    return {
        "style_label": humanize_song_style(style_code),
        "style_details": style_details,
        "singer_label": humanize_singer_gender(order.singer_gender),
        "mood_label": humanize_song_mood(order.song_mood),
    }


def get_latest_payment(order: Order):
    if not order.payments:
        return None
    return sorted(order.payments, key=lambda item: item.id or 0, reverse=True)[0]


def has_successful_payment(order: Order) -> bool:
    return any(payment.status == "succeeded" for payment in order.payments)


def can_pay_order(order: Order) -> bool:
    return bool((order.final_lyrics_text or "").strip()) and not has_successful_payment(order)


def get_order_pricing_context(db: Session, order: Order) -> dict[str, int | bool]:
    latest_payment = get_latest_payment(order)
    if latest_payment is not None:
        return {
            "current_amount_rub": latest_payment.final_amount_rub,
            "base_amount_rub": latest_payment.base_amount_rub,
            "discount_amount_rub": latest_payment.discount_amount_rub,
            "has_discount": latest_payment.has_discount,
        }

    preview = build_order_pricing_preview(db, order)
    return {
        "current_amount_rub": int(preview["final_price_rub"]),
        "base_amount_rub": int(preview["base_price_rub"]),
        "discount_amount_rub": int(preview["discount_rub"]),
        "has_discount": bool(preview["has_discount"]),
    }


def get_payment_cta_label(db: Session, order: Order) -> str:
    latest_payment = get_latest_payment(order)
    if latest_payment and latest_payment.status in {"pending", "waiting_for_capture"}:
        return "Продолжить оплату"

    pricing = get_order_pricing_context(db, order)
    return f"Оплатить {pricing['current_amount_rub']} ₽"


def can_start_song(order: Order) -> bool:
    return settings.SUNO_STUB_MODE or order.status == "paid"


def build_magic_login_url(raw_token: str) -> str:
    return f"{settings.BASE_URL.rstrip('/')}/account/magic-login?token={raw_token}"


@router.get("/login", response_class=HTMLResponse)
async def account_login_page(request: Request):
    sent = request.query_params.get("sent") == "1"
    stub_login_url = request.session.get("stub_account_login_url")

    return templates.TemplateResponse(
        "account/login.html",
        {
            "request": request,
            "page_title": "Вход в кабинет",
            "sent": sent,
            "stub_mode": settings.MAGIC_LINK_STUB_MODE,
            "stub_login_url": stub_login_url,
            "error": None,
        },
    )


@router.post("/login", response_class=HTMLResponse)
async def account_login_submit(
    request: Request,
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    email = normalize_email(email)
    if not is_valid_email(email):
        return templates.TemplateResponse(
            "account/login.html",
            {
                "request": request,
                "page_title": "Вход в кабинет",
                "sent": False,
                "stub_mode": settings.MAGIC_LINK_STUB_MODE,
                "stub_login_url": None,
                "error": "Укажи корректный email.",
            },
            status_code=400,
        )

    user = db.query(User).filter(User.email == email).first()

    limit_decision = enforce_rate_limit(
        db,
        request=request,
        action="account_magic_link_send",
        user_message="Ссылка для входа уже запрашивалась слишком часто. Подождите немного и попробуйте снова.",
        rules=[
            RateLimitRule("ip", get_client_ip(request), settings.MAGIC_LINK_IP_LIMIT_PER_HOUR, 60 * 60),
            RateLimitRule("email", email, settings.MAGIC_LINK_EMAIL_LIMIT_PER_HOUR, 60 * 60),
        ],
        extra_payload={"email": email, "user_exists": bool(user)},
    )
    if not limit_decision.allowed:
        db.commit()
        return templates.TemplateResponse(
            "account/login.html",
            {
                "request": request,
                "page_title": "Вход в кабинет",
                "sent": False,
                "stub_mode": settings.MAGIC_LINK_STUB_MODE,
                "stub_login_url": None,
                "error": limit_decision.message,
            },
            status_code=429,
        )

    db.commit()

    if user:
        raw_token = generate_magic_token()
        token_hash = hash_magic_token(raw_token)
        expires_at = utcnow() + timedelta(minutes=settings.MAGIC_LINK_TTL_MINUTES)

        db.add(
            MagicLoginToken(
                user_id=user.id,
                token_hash=token_hash,
                expires_at=expires_at,
            )
        )
        db.commit()

        login_url = build_magic_login_url(raw_token)

        try:
            delivery = send_magic_link_email(
                recipient_email=email,
                login_url=login_url,
            )
            create_email_log(
                db,
                email_type="magic_link",
                recipient_email=email,
                subject=magic_link_email_subject(),
                status="stub" if delivery.mode == "stub" else "sent",
                delivery_mode=delivery.mode,
                user=user,
                payload={"login_url": delivery.login_url},
            )
            db.commit()

        except EmailServiceError as exc:
            create_email_log(
                db,
                email_type="magic_link",
                recipient_email=email,
                subject=magic_link_email_subject(),
                status="failed",
                delivery_mode="email",
                user=user,
                error_message=str(exc),
                payload={"login_url": login_url},
            )
            db.commit()
            return templates.TemplateResponse(
                "account/login.html",
                {
                    "request": request,
                    "page_title": "Вход в кабинет",
                    "sent": False,
                    "stub_mode": settings.MAGIC_LINK_STUB_MODE,
                    "stub_login_url": None,
                    "error": str(exc),
                },
                status_code=400,
            )

        if delivery.mode == "stub":
            request.session["stub_account_login_url"] = delivery.login_url

    return RedirectResponse(
        url=f"{request.url_for('account_login_page')}?sent=1",
        status_code=303,
    )


@router.get("/magic-login")
async def account_magic_login(
    request: Request,
    token: str,
    db: Session = Depends(get_db),
):
    token_hash = hash_magic_token(token)

    magic_token = (
        db.query(MagicLoginToken)
        .filter(MagicLoginToken.token_hash == token_hash)
        .first()
    )

    now = utcnow()

    if (
        magic_token is None
        or magic_token.used_at is not None
        or magic_token.expires_at < now
    ):
        return RedirectResponse(
            url=request.url_for("account_login_page"),
            status_code=303,
        )

    magic_token.used_at = now
    request.session["account_user_id"] = magic_token.user_id
    request.session.pop("stub_account_login_url", None)
    request.session.pop("stub_questionnaire_login_url", None)
    db.commit()

    return RedirectResponse(
        url=request.url_for("account_dashboard"),
        status_code=303,
    )


@router.get("/logout")
async def account_logout(request: Request):
    request.session.pop("account_user_id", None)
    return RedirectResponse(url="/", status_code=303)


@router.post("/orders/{order_public_id}/title")
async def account_update_order_title(
    order_public_id: str,
    request: Request,
    title: str = Form(""),
    db: Session = Depends(get_db),
):
    user = get_session_user(request, db)
    if user is None:
        return RedirectResponse(url=request.url_for("account_login_page"), status_code=303)

    order = (
        db.query(Order)
        .filter(
            Order.public_id == order_public_id,
            Order.user_id == user.id,
        )
        .first()
    )
    if order is None:
        return RedirectResponse(url=request.url_for("account_dashboard"), status_code=303)

    normalized_title = " ".join((title or "").strip().split())
    order.title = normalized_title[:255] if normalized_title else None
    db.commit()

    return RedirectResponse(url=request.url_for("account_dashboard"), status_code=303)


@router.get("/", response_class=HTMLResponse)
async def account_dashboard(request: Request, db: Session = Depends(get_db)):
    user = get_session_user(request, db)
    if user is None:
        return RedirectResponse(url=request.url_for("account_login_page"), status_code=303)

    orders = (
        db.query(Order)
        .filter(Order.user_id == user.id)
        .order_by(Order.id.desc())
        .all()
    )

    order_cards = []
    for order in orders:
        latest_payment = get_latest_payment(order)
        latest_song = get_latest_song(order)
        latest_ready_song = get_latest_ready_song(order)
        song_attempts = get_song_attempts(order)
        song_profile = build_song_profile(order)

        pricing = get_order_pricing_context(db, order)
        playback_song = latest_ready_song
        has_previous_ready_song = bool(
            playback_song and latest_song and playback_song.public_id != latest_song.public_id
        )
        ready_variants_count = 0
        if playback_song is not None:
            ready_variants_count = len(playback_song.audio_variants) if playback_song.audio_variants else (1 if playback_song.audio_url else 0)

        order_cards.append(
            {
                "order": order,
                "latest_payment": latest_payment,
                "latest_song": latest_song,
                "latest_ready_song": latest_ready_song,
                "playback_song": playback_song,
                "song_attempts": song_attempts,
                "has_previous_ready_song": has_previous_ready_song,
                "payment_status_label": humanize_payment_status(latest_payment.status if latest_payment else None),
                "song_status_label": humanize_song_status(latest_song.status if latest_song else None),
                "song_style_label": song_profile["style_label"],
                "song_style_details": song_profile["style_details"],
                "singer_label": song_profile["singer_label"],
                "mood_label": song_profile["mood_label"],
                "can_pay_order": can_pay_order(order),
                "payment_cta_label": get_payment_cta_label(db, order),
                "can_start_song": can_start_song(order),
                "song_is_running": latest_song is not None and latest_song.status in RUNNING_SONG_STATUSES,
                "song_is_ready": latest_song is not None and latest_song.status == "succeeded",
                "song_is_failed": latest_song is not None and latest_song.status == "failed",
                "has_ready_song": playback_song is not None,
                "ready_variants_count": ready_variants_count,
                **pricing,
            }
        )

    return templates.TemplateResponse(
        "account/dashboard.html",
        {
            "request": request,
            "page_title": "Личный кабинет",
            "user": user,
            "orders": orders,
            "order_cards": order_cards,
            "suno_stub_mode": settings.SUNO_STUB_MODE,
        },
    )


@router.get("/orders/{order_public_id}", response_class=HTMLResponse)
async def account_order_detail(
    order_public_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    user = get_session_user(request, db)
    if user is None:
        return RedirectResponse(url=request.url_for("account_login_page"), status_code=303)

    order = (
        db.query(Order)
        .filter(
            Order.public_id == order_public_id,
            Order.user_id == user.id,
        )
        .first()
    )

    if order is None:
        return RedirectResponse(url=request.url_for("account_dashboard"), status_code=303)

    latest_payment = get_latest_payment(order)
    refresh_payment = request.query_params.get("refresh_payment") == "1"
    if refresh_payment and latest_payment and latest_payment.yookassa_payment_id and latest_payment.status not in FINAL_PAYMENT_STATUSES:
        try:
            sync_payment_with_remote(
                db,
                latest_payment,
                trigger="account_order_detail_open",
                event_name="payment_status_synced_from_order_detail",
            )
            db.commit()
            db.refresh(order)
            latest_payment = get_latest_payment(order)
        except Exception:
            db.rollback()
            order = (
                db.query(Order)
                .filter(
                    Order.public_id == order_public_id,
                    Order.user_id == user.id,
                )
                .first()
            )
            if order is None:
                return RedirectResponse(url=request.url_for("account_dashboard"), status_code=303)
            latest_payment = get_latest_payment(order)
    latest_song = get_latest_song(order)
    song_sync_error = None
    if latest_song is not None and latest_song.status in RUNNING_SONG_STATUSES:
        try:
            latest_song = sync_song_job_state(db, latest_song, event_type="song_generation_status_changed_from_account_order")
            db.commit()
            db.refresh(order)
            latest_song = get_latest_song(order)
        except SunoServiceError as exc:
            song_sync_error = str(exc)
            db.rollback()
            order = (
                db.query(Order)
                .filter(
                    Order.public_id == order_public_id,
                    Order.user_id == user.id,
                )
                .first()
            )
            if order is None:
                return RedirectResponse(url=request.url_for("account_dashboard"), status_code=303)
            latest_song = get_latest_song(order)

    latest_ready_song = get_latest_ready_song(order)
    song_attempts = get_song_attempts(order)
    playback_song = latest_ready_song
    has_previous_ready_song = bool(playback_song and latest_song and playback_song.public_id != latest_song.public_id)
    song_profile = build_song_profile(order)
    pricing = get_order_pricing_context(db, order)

    welcome = request.query_params.get("welcome") == "1"
    delivery = (request.query_params.get("delivery") or "").strip().lower()

    return templates.TemplateResponse(
        "account/order_detail.html",
        {
            "request": request,
            "page_title": f"Заказ {order.order_number}",
            "user": user,
            "order": order,
            "latest_payment": latest_payment,
            "latest_song": latest_song,
            "latest_ready_song": latest_ready_song,
            "playback_song": playback_song,
            "song_attempts": song_attempts,
            "has_previous_ready_song": has_previous_ready_song,
            "payment_status_label": humanize_payment_status(latest_payment.status if latest_payment else None),
            "song_status_label": humanize_song_status(latest_song.status if latest_song else None),
            "song_style_label": song_profile["style_label"],
            "song_style_details": song_profile["style_details"],
            "singer_label": song_profile["singer_label"],
            "mood_label": song_profile["mood_label"],
            "can_pay_order": can_pay_order(order),
            "payment_cta_label": get_payment_cta_label(db, order),
            **pricing,
            "can_start_song": can_start_song(order),
            "song_is_running": latest_song is not None and latest_song.status in RUNNING_SONG_STATUSES,
            "song_is_ready": latest_song is not None and latest_song.status == "succeeded",
            "song_is_failed": latest_song is not None and latest_song.status == "failed",
            "suno_stub_mode": settings.SUNO_STUB_MODE,
            "welcome": welcome,
            "welcome_delivery": delivery,
            "song_sync_error": song_sync_error,
        },
    )
