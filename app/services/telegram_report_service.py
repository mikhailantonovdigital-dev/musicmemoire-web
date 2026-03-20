from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta, timezone
import json
from urllib import error, request

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Order, OrderPayment, User
from app.models.support_thread import SupportMessage, SupportThread


TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_REPORT_BUTTON = "Отчёт"
TELEGRAM_REPORT_KEYBOARD = {"keyboard": [[{"text": TELEGRAM_REPORT_BUTTON}]], "resize_keyboard": True}


@dataclass(slots=True)
class TelegramReportResult:
    ok: bool
    detail: str


def telegram_reporting_enabled() -> bool:
    return bool((settings.TELEGRAM_BOT_TOKEN or "").strip() and (settings.TELEGRAM_REPORT_CHAT_ID or "").strip())


def send_telegram_message(*, chat_id: int | str, text: str, reply_markup: dict | None = None) -> TelegramReportResult:
    bot_token = (settings.TELEGRAM_BOT_TOKEN or "").strip()
    if not bot_token or not str(chat_id).strip():
        return TelegramReportResult(ok=False, detail="TELEGRAM_BOT_TOKEN или chat_id не заданы.")

    payload_dict = {
        "chat_id": str(chat_id).strip(),
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload_dict["reply_markup"] = reply_markup

    payload = json.dumps(payload_dict).encode("utf-8")
    req = request.Request(
        url=f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=15) as response:
            raw = response.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        return TelegramReportResult(ok=False, detail=f"HTTP {exc.code}: {detail or exc.reason}")
    except error.URLError as exc:
        return TelegramReportResult(ok=False, detail=f"Ошибка сети: {exc.reason}")

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return TelegramReportResult(ok=False, detail="Telegram вернул некорректный JSON.")

    if not parsed.get("ok"):
        return TelegramReportResult(ok=False, detail=str(parsed.get("description") or "Неизвестная ошибка Telegram API."))
    return TelegramReportResult(ok=True, detail="Отчёт отправлен в Telegram.")


def send_telegram_report(text: str) -> TelegramReportResult:
    chat_id = (settings.TELEGRAM_REPORT_CHAT_ID or "").strip()
    if not chat_id:
        return TelegramReportResult(ok=False, detail="TELEGRAM_REPORT_CHAT_ID не задан.")
    return send_telegram_message(chat_id=chat_id, text=text)


def _escape(value: str | None) -> str:
    return (value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _base_lines(thread: SupportThread) -> list[str]:
    order_public_id = thread.order.public_id if thread.order else "—"
    order_number = thread.order.order_number if thread.order else "—"
    return [
        "<b>Magic Music · support report</b>",
        f"Thread: <code>{_escape(thread.public_id)}</code>",
        f"Status: <b>{_escape(thread.status)}</b>",
        f"Email: {_escape(thread.email) or '—'}",
        f"Order: {_escape(order_number)} · {_escape(order_public_id)}",
        f"Source: {_escape(thread.source)}",
    ]


def notify_new_support_thread(thread: SupportThread, message: SupportMessage) -> TelegramReportResult:
    lines = _base_lines(thread)
    if thread.subject:
        lines.append(f"Subject: {_escape(thread.subject)}")
    if message.attachment_original_filename:
        lines.append(f"Attachment: {_escape(message.attachment_original_filename)}")
    lines.extend([
        "",
        "<b>New support message</b>",
        _escape(message.body),
    ])
    return send_telegram_report("\n".join(lines))


def notify_admin_support_reply(thread: SupportThread, message: SupportMessage) -> TelegramReportResult:
    lines = _base_lines(thread)
    lines.extend([
        "",
        f"<b>Admin {'internal note' if message.is_internal else 'reply'}</b>",
        _escape(message.body),
    ])
    return send_telegram_report("\n".join(lines))


def build_test_report() -> str:
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return "\n".join(
        [
            "<b>Magic Music · test report</b>",
            f"Generated: {generated_at}",
            "If you received this message, Telegram reporting is configured correctly.",
        ]
    )


def _today_range_utc() -> tuple[datetime, datetime]:
    now_utc = datetime.now(timezone.utc)
    start_utc = datetime.combine(now_utc.date(), time.min, tzinfo=timezone.utc)
    end_utc = start_utc + timedelta(days=1)
    return start_utc, end_utc


def build_daily_metrics_report(db: Session) -> str:
    start_utc, end_utc = _today_range_utc()

    orders_today = db.query(func.count(Order.id)).filter(Order.created_at >= start_utc, Order.created_at < end_utc).scalar() or 0
    users_today = db.query(func.count(User.id)).filter(User.created_at >= start_utc, User.created_at < end_utc).scalar() or 0
    total_users = db.query(func.count(User.id)).scalar() or 0
    payments_today = db.query(OrderPayment).filter(OrderPayment.status == "succeeded", OrderPayment.paid_at.is_not(None), OrderPayment.paid_at >= start_utc, OrderPayment.paid_at < end_utc).all()
    successful_payments_today = len(payments_today)
    payments_sum_today = sum(item.final_amount_rub for item in payments_today)

    return "\n".join([
        "<b>Magic Music · отчёт за сегодня</b>",
        f"Дата (UTC): {start_utc.strftime('%Y-%m-%d')}",
        "",
        f"Заказов сегодня: <b>{int(orders_today)}</b>",
        f"Успешных оплат сегодня: <b>{int(successful_payments_today)}</b>",
        f"Сумма успешных оплат сегодня: <b>{int(payments_sum_today)} ₽</b>",
        f"Новых пользователей сегодня: <b>{int(users_today)}</b>",
        f"Уникальных пользователей за всё время: <b>{int(total_users)}</b>",
    ])
