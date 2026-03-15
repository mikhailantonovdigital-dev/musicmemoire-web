from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

from app.core.config import settings


class EmailServiceError(RuntimeError):
    pass


@dataclass(slots=True)
class MagicLinkDeliveryResult:
    mode: str
    login_url: str


def send_magic_link_email(*, recipient_email: str, login_url: str) -> MagicLinkDeliveryResult:
    if settings.MAGIC_LINK_STUB_MODE:
        return MagicLinkDeliveryResult(mode="stub", login_url=login_url)

    if not settings.SMTP_HOST or not settings.SMTP_USER or not settings.SMTP_PASSWORD:
        raise EmailServiceError("SMTP не настроен. Проверь SMTP_HOST, SMTP_USER и SMTP_PASSWORD.")

    from_email = settings.SMTP_FROM_EMAIL or settings.SMTP_USER
    subject = "Вход в личный кабинет Magic Moment"

    text_body = f"""Здравствуйте!

Ваш заказ сохранён в Magic Moment.

Чтобы войти в личный кабинет, откройте ссылку:
{login_url}

Ссылка действует ограниченное время.

Если это были не вы, просто проигнорируйте письмо.
"""

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{settings.SMTP_FROM_NAME} <{from_email}>"
    msg["To"] = recipient_email
    msg.set_content(text_body)

    try:
        if settings.SMTP_PORT == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, context=context) as server:
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                server.send_message(msg)
        else:
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
                server.starttls(context=ssl.create_default_context())
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
                server.send_message(msg)
    except Exception as exc:
        raise EmailServiceError("Не удалось отправить письмо со ссылкой для входа.") from exc

    return MagicLinkDeliveryResult(mode="email", login_url=login_url)
