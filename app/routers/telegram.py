from __future__ import annotations

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.services.telegram_report_service import (
    TELEGRAM_REPORT_BUTTON,
    TELEGRAM_REPORT_KEYBOARD,
    build_daily_metrics_report,
    send_telegram_message,
)

router = APIRouter(prefix="/telegram", tags=["telegram"])


def _message_chat_id(update: dict) -> int | None:
    message = update.get("message") or update.get("callback_query", {}).get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    return int(chat_id) if isinstance(chat_id, int) else None


def _message_text(update: dict) -> str:
    message = update.get("message") or {}
    text = message.get("text")
    return text.strip() if isinstance(text, str) else ""


@router.post("/webhook")
async def telegram_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    expected_secret = (settings.TELEGRAM_BOT_WEBHOOK_SECRET or "").strip()
    if expected_secret and x_telegram_bot_api_secret_token != expected_secret:
        return Response(status_code=403)

    update = await request.json()
    chat_id = _message_chat_id(update)
    if chat_id is None:
        return {"ok": True}

    text = _message_text(update)
    normalized = text.casefold()
    if text.startswith("/start") or normalized in {"/menu", "/report", "/otchet", "отчёт", "отчет"}:
        if text.startswith("/start") or normalized == "/menu":
            send_telegram_message(
                chat_id=chat_id,
                text="Нажмите кнопку «Отчёт», и я пришлю сводку по заказам, оплатам и пользователям за сегодня.",
                reply_markup=TELEGRAM_REPORT_KEYBOARD,
            )
        else:
            send_telegram_message(
                chat_id=chat_id,
                text=build_daily_metrics_report(db),
                reply_markup=TELEGRAM_REPORT_KEYBOARD,
            )
        return {"ok": True}

    send_telegram_message(
        chat_id=chat_id,
        text=f"Пока я понимаю только кнопку «{TELEGRAM_REPORT_BUTTON}». Нажмите её, чтобы получить сводку за сегодня.",
        reply_markup=TELEGRAM_REPORT_KEYBOARD,
    )
    return {"ok": True}
