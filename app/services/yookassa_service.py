from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from app.core.config import settings

YOOKASSA_API_BASE = "https://api.yookassa.ru/v3"


class YooKassaError(RuntimeError):
    pass


@dataclass(slots=True)
class YooKassaPaymentInfo:
    id: str
    status: str
    paid: bool
    confirmation_url: str | None
    amount_value: str
    currency: str
    metadata: dict
    raw: dict


@dataclass(slots=True)
class YooKassaCreateResult:
    payment: YooKassaPaymentInfo
    idempotence_key: str


def _build_auth_header() -> str:
    if not settings.YOOKASSA_SHOP_ID or not settings.YOOKASSA_SECRET_KEY:
        raise YooKassaError("ЮKassa не настроена. Проверь YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY.")

    raw = f"{settings.YOOKASSA_SHOP_ID}:{settings.YOOKASSA_SECRET_KEY}".encode("utf-8")
    token = base64.b64encode(raw).decode("ascii")
    return f"Basic {token}"


def _request_json(method: str, path: str, *, body: dict | None = None, idempotence_key: str | None = None) -> dict:
    headers = {
        "Authorization": _build_auth_header(),
        "Content-Type": "application/json",
    }
    if idempotence_key:
        headers["Idempotence-Key"] = idempotence_key

    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    request = Request(
        url=f"{YOOKASSA_API_BASE}{path}",
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="ignore")
        try:
            error_data = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            error_data = {}
        description = error_data.get("description") or error_data.get("code") or exc.reason
        raise YooKassaError(f"Ошибка ЮKassa: {description}") from exc
    except URLError as exc:
        raise YooKassaError("Не удалось соединиться с ЮKassa.") from exc


def _parse_payment(data: dict) -> YooKassaPaymentInfo:
    confirmation = data.get("confirmation") or {}
    amount = data.get("amount") or {}
    return YooKassaPaymentInfo(
        id=str(data.get("id") or ""),
        status=str(data.get("status") or "pending"),
        paid=bool(data.get("paid") or False),
        confirmation_url=confirmation.get("confirmation_url"),
        amount_value=str(amount.get("value") or "0.00"),
        currency=str(amount.get("currency") or "RUB"),
        metadata=data.get("metadata") or {},
        raw=data,
    )


def _format_amount_rub(amount_rub: int) -> str:
    return str(Decimal(amount_rub).quantize(Decimal("0.00")))

def _build_receipt(*, amount_rub: int, customer_email: str | None) -> dict | None:
    email = (customer_email or settings.YOOKASSA_RECEIPT_EMAIL or "").strip()
    if not email:
        return None

    item = {
        "description": "Персональная песня Magic Music",
        "quantity": "1.00",
        "amount": {
            "value": _format_amount_rub(amount_rub),
            "currency": "RUB",
        },
        "payment_mode": "full_payment",
        "payment_subject": "service",
    }

    vat_code = (settings.YOOKASSA_VAT_CODE or "").strip()
    if vat_code:
        if not vat_code.isdigit():
            raise YooKassaError("YOOKASSA_VAT_CODE должен быть числом.")
        item["vat_code"] = int(vat_code)

    receipt = {
        "customer": {
            "email": email,
        },
        "items": [item],
    }

    tax_system_code = (settings.YOOKASSA_TAX_SYSTEM_CODE or "").strip()
    if tax_system_code:
        if not tax_system_code.isdigit():
            raise YooKassaError("YOOKASSA_TAX_SYSTEM_CODE должен быть числом.")
        receipt["tax_system_code"] = int(tax_system_code)

    return receipt


def create_redirect_payment(
    *,
    order_number: str,
    order_public_id: str,
    user_public_id: str | None,
    payment_public_id: str,
    amount_rub: int,
    return_url: str,
    customer_email: str | None = None,
) -> YooKassaCreateResult:
    idempotence_key = str(uuid4())

    payload = {
        "amount": {
            "value": _format_amount_rub(amount_rub),
            "currency": "RUB",
        },
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": return_url,
        },
        "description": f"Magic Music · {order_number}",
        "metadata": {
            "order_public_id": order_public_id,
            "payment_public_id": payment_public_id,
            "order_number": order_number,
            "user_public_id": user_public_id or "",
        },
    }

    receipt = _build_receipt(amount_rub=amount_rub, customer_email=customer_email)
    if receipt:
        payload["receipt"] = receipt

    data = _request_json(
        "POST",
        "/payments",
        body=payload,
        idempotence_key=idempotence_key,
    )

    return YooKassaCreateResult(
        payment=_parse_payment(data),
        idempotence_key=idempotence_key,
    )


def fetch_payment(payment_id: str) -> YooKassaPaymentInfo:
    data = _request_json("GET", f"/payments/{payment_id}")
    return _parse_payment(data)
