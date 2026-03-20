from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from urllib import error, request

from app.core.config import settings
from app.models.support_thread import SupportMessage, SupportThread


TELEGRAM_API_BASE = "https://api.telegram.org"


@dataclass(slots=True)
class TelegramReportResult:
    ok: bool
    detail: str


def telegram_reporting_enabled() -> bool:
    return bool((settings.TELEGRAM_BOT_TOKEN or "").strip() and (settings.TELEGRAM_REPORT_CHAT_ID or "").strip())


def send_telegram_report(text: str) -> TelegramReportResult:
    bot_token = (settings.TELEGRAM_BOT_TOKEN or "").strip()
    chat_id = (settings.TELEGRAM_REPORT_CHAT_ID or "").strip()
    if not bot_token or not chat_id:
        return TelegramReportResult(ok=False, detail="TELEGRAM_BOT_TOKEN или TELEGRAM_REPORT_CHAT_ID не заданы.")

    payload = json.dumps(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
    ).encode("utf-8")
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
