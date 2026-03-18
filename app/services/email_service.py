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


def _smtp_settings() -> tuple[str, int, str, str, str]:
    if not settings.SMTP_HOST or not settings.SMTP_USER or not settings.SMTP_PASSWORD:
        raise EmailServiceError("SMTP не настроен. Проверь SMTP_HOST, SMTP_USER и SMTP_PASSWORD.")

    from_email = (settings.SMTP_FROM_EMAIL or settings.SMTP_USER or "").strip()
    if not from_email:
        raise EmailServiceError("SMTP не настроен. Проверь SMTP_FROM_EMAIL.")

    return (
        settings.SMTP_HOST.strip(),
        int(settings.SMTP_PORT),
        settings.SMTP_USER.strip(),
        settings.SMTP_PASSWORD,
        from_email,
    )


def _deliver_email(*, recipient_email: str, subject: str, text_body: str, html_body: str | None = None) -> None:
    smtp_host, smtp_port, smtp_user, smtp_password, from_email = _smtp_settings()

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{settings.SMTP_FROM_NAME} <{from_email}>"
    msg["To"] = recipient_email.strip()
    msg.set_content(text_body)

    if html_body:
        msg.add_alternative(html_body, subtype="html")

    try:
        if smtp_port == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as server:
                server.login(smtp_user, smtp_password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                server.login(smtp_user, smtp_password)
                server.send_message(msg)
    except Exception as exc:
        raise EmailServiceError("Не удалось отправить письмо.") from exc


def send_magic_link_email(*, recipient_email: str, login_url: str) -> MagicLinkDeliveryResult:
    if settings.MAGIC_LINK_STUB_MODE:
        return MagicLinkDeliveryResult(mode="stub", login_url=login_url)

    subject = "Вход в личный кабинет Magic Music"

    text_body = f"""Здравствуйте!

Ваш заказ сохранён в Magic Music.

Чтобы войти в личный кабинет, откройте ссылку:
{login_url}

Ссылка действует ограниченное время.

Если это были не вы, просто проигнорируйте письмо.
"""

    html_body = f"""\
<!doctype html>
<html lang="ru">
  <body style="margin:0;padding:0;background:#0a1220;color:#f5f7ff;font-family:Arial,sans-serif;">
    <div style="max-width:640px;margin:0 auto;padding:32px 20px;">
      <div style="padding:28px;border-radius:24px;background:linear-gradient(135deg,#101b31 0%,#172542 52%,#1a3158 100%);border:1px solid rgba(160,181,220,0.18);box-shadow:0 24px 60px rgba(4,12,28,0.28);">
        <div style="font-size:12px;line-height:1.4;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#b8c8ff;margin-bottom:14px;">
          Magic Music
        </div>

        <h1 style="margin:0 0 14px;font-size:28px;line-height:1.1;color:#ffffff;">
          Вход в личный кабинет
        </h1>

        <p style="margin:0 0 14px;font-size:16px;line-height:1.6;color:#dfe8ff;">
          Ваш заказ сохранён. Чтобы открыть кабинет и продолжить работу с заказом, перейдите по кнопке ниже.
        </p>

        <div style="margin:24px 0 22px;">
          <a href="{login_url}" style="display:inline-block;padding:14px 22px;border-radius:14px;background:linear-gradient(135deg,#6d7cff 0%,#8d66ff 100%);color:#ffffff;text-decoration:none;font-size:16px;font-weight:700;">
            Войти в кабинет
          </a>
        </div>

        <p style="margin:0 0 12px;font-size:14px;line-height:1.6;color:#c8d6fb;">
          Если кнопка не открывается, используйте прямую ссылку:
        </p>

        <p style="margin:0 0 18px;font-size:14px;line-height:1.6;word-break:break-all;">
          <a href="{login_url}" style="color:#a9beff;">{login_url}</a>
        </p>

        <p style="margin:0;font-size:13px;line-height:1.6;color:#9fb0d9;">
          Ссылка действует ограниченное время. Если это были не вы, просто проигнорируйте письмо.
        </p>
      </div>
    </div>
  </body>
</html>
"""

    _deliver_email(
        recipient_email=recipient_email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
    )
    return MagicLinkDeliveryResult(mode="email", login_url=login_url)


def send_payment_success_email(*, recipient_email: str, order_number: str, order_url: str, price_rub: int) -> None:
    subject = f"Оплата прошла — заказ {order_number}"

    text_body = f"""Здравствуйте!

Оплата заказа {order_number} прошла успешно.

Сумма: {price_rub} ₽
Открыть заказ:
{order_url}

Теперь заказ доступен в кабинете, и можно переходить к следующему этапу.
"""

    html_body = f"""\
<!doctype html>
<html lang="ru">
  <body style="margin:0;padding:0;background:#0a1220;color:#f5f7ff;font-family:Arial,sans-serif;">
    <div style="max-width:640px;margin:0 auto;padding:32px 20px;">
      <div style="padding:28px;border-radius:24px;background:linear-gradient(135deg,#101b31 0%,#172542 52%,#1a3158 100%);border:1px solid rgba(160,181,220,0.18);box-shadow:0 24px 60px rgba(4,12,28,0.28);">
        <div style="font-size:12px;line-height:1.4;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#b8c8ff;margin-bottom:14px;">
          Magic Music
        </div>

        <h1 style="margin:0 0 14px;font-size:28px;line-height:1.1;color:#ffffff;">
          Оплата прошла успешно
        </h1>

        <p style="margin:0 0 10px;font-size:16px;line-height:1.6;color:#dfe8ff;">
          Заказ <strong>{order_number}</strong> оплачен.
        </p>

        <p style="margin:0 0 18px;font-size:16px;line-height:1.6;color:#dfe8ff;">
          Сумма: <strong>{price_rub} ₽</strong>
        </p>

        <div style="margin:24px 0 22px;">
          <a href="{order_url}" style="display:inline-block;padding:14px 22px;border-radius:14px;background:linear-gradient(135deg,#6d7cff 0%,#8d66ff 100%);color:#ffffff;text-decoration:none;font-size:16px;font-weight:700;">
            Открыть заказ
          </a>
        </div>

        <p style="margin:0;font-size:13px;line-height:1.6;color:#9fb0d9;">
          Заказ сохранён в кабинете. Ссылка ведёт прямо в карточку заказа.
        </p>
      </div>
    </div>
  </body>
</html>
"""

    _deliver_email(
        recipient_email=recipient_email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
    )


def send_song_ready_email(*, recipient_email: str, order_number: str, order_url: str, audio_url: str | None = None) -> None:
    subject = f"Песня готова — заказ {order_number}"

    extra_text = f"\nСсылка на аудио:\n{audio_url}\n" if audio_url else ""

    text_body = f"""Здравствуйте!

Песня по заказу {order_number} готова.

Открыть заказ:
{order_url}{extra_text}
В кабинете можно прослушать результат и перейти к деталям заказа.
"""

    audio_link_block = (
        f"""
        <p style="margin:0 0 18px;font-size:14px;line-height:1.6;color:#c8d6fb;">
          Прямая ссылка на аудио:
          <br />
          <a href="{audio_url}" style="color:#a9beff;word-break:break-all;">{audio_url}</a>
        </p>
        """
        if audio_url
        else ""
    )

    html_body = f"""\
<!doctype html>
<html lang="ru">
  <body style="margin:0;padding:0;background:#0a1220;color:#f5f7ff;font-family:Arial,sans-serif;">
    <div style="max-width:640px;margin:0 auto;padding:32px 20px;">
      <div style="padding:28px;border-radius:24px;background:linear-gradient(135deg,#101b31 0%,#172542 52%,#1a3158 100%);border:1px solid rgba(160,181,220,0.18);box-shadow:0 24px 60px rgba(4,12,28,0.28);">
        <div style="font-size:12px;line-height:1.4;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#b8c8ff;margin-bottom:14px;">
          Magic Music
        </div>

        <h1 style="margin:0 0 14px;font-size:28px;line-height:1.1;color:#ffffff;">
          Песня готова
        </h1>

        <p style="margin:0 0 14px;font-size:16px;line-height:1.6;color:#dfe8ff;">
          Заказ <strong>{order_number}</strong> успешно завершён. Результат уже доступен в кабинете.
        </p>

        <div style="margin:24px 0 22px;">
          <a href="{order_url}" style="display:inline-block;padding:14px 22px;border-radius:14px;background:linear-gradient(135deg,#6d7cff 0%,#8d66ff 100%);color:#ffffff;text-decoration:none;font-size:16px;font-weight:700;">
            Открыть заказ
          </a>
        </div>

        {audio_link_block}

        <p style="margin:0;font-size:13px;line-height:1.6;color:#9fb0d9;">
          В кабинете можно прослушать готовую песню и открыть карточку заказа.
        </p>
      </div>
    </div>
  </body>
</html>
"""

    _deliver_email(
        recipient_email=recipient_email,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
    )
