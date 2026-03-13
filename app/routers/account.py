from __future__ import annotations

import re
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.security import generate_magic_token, hash_magic_token, utcnow
from app.models import MagicLoginToken, Order, User
from app.services.email_service import EmailServiceError, send_magic_link_email

templates = Jinja2Templates(directory="app/templates")

router = APIRouter(prefix="/account", tags=["account"])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(value: str) -> str:
    return value.strip().lower()


def is_valid_email(value: str) -> bool:
    return bool(EMAIL_RE.match(value.strip()))


def get_session_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("account_user_id")
    if not user_id:
        return None
    return db.query(User).filter(User.id == int(user_id)).first()


def humanize_payment_status(status: str | None) -> str:
    mapping = {
        "pending": "Ожидает оплаты",
        "waiting_for_capture": "Ожидает подтверждения",
        "succeeded": "Оплачено",
        "canceled": "Не оплачено",
    }
    return mapping.get(status or "", "Не начата")


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

        login_url = f"{settings.BASE_URL}/account/magic-login?token={raw_token}"

        try:
            delivery = send_magic_link_email(
                recipient_email=email,
                login_url=login_url,
            )
        except EmailServiceError:
            return templates.TemplateResponse(
                "account/login.html",
                {
                    "request": request,
                    "page_title": "Вход в кабинет",
                    "sent": False,
                    "stub_mode": settings.MAGIC_LINK_STUB_MODE,
                    "stub_login_url": None,
                    "error": "Не удалось отправить письмо со ссылкой для входа.",
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
    for item in orders:
        latest_payment = item.payments[0] if item.payments else None
        order_cards.append(
            {
                "order": item,
                "latest_payment": latest_payment,
                "payment_status_label": humanize_payment_status(latest_payment.status if latest_payment else None),
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
            "price_rub": settings.PRICE_RUB,
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

    latest_payment = order.payments[0] if order.payments else None

    return templates.TemplateResponse(
        "account/order_detail.html",
        {
            "request": request,
            "page_title": f"Заказ {order.order_number}",
            "user": user,
            "order": order,
            "latest_payment": latest_payment,
            "payment_status_label": humanize_payment_status(latest_payment.status if latest_payment else None),
            "price_rub": settings.PRICE_RUB,
        },
    )
