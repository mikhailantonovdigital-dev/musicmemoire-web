from __future__ import annotations

import json
from datetime import date, datetime
from urllib import parse, request
from urllib.error import HTTPError, URLError

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Order

METRICA_API_BASE = "https://api-metrika.yandex.net/stat/v1/data"


def metrica_reporting_enabled() -> bool:
    return bool((settings.METRICA_COUNTER_ID or "").strip() and (settings.METRICA_API_TOKEN or "").strip())


def _request_metrica_users(*, date_from: date, date_to: date) -> int | None:
    if not metrica_reporting_enabled():
        return None

    query = parse.urlencode(
        {
            "ids": str(settings.METRICA_COUNTER_ID).strip(),
            "metrics": "ym:s:users",
            "date1": date_from.isoformat(),
            "date2": date_to.isoformat(),
            "accuracy": "full",
        }
    )

    req = request.Request(
        url=f"{METRICA_API_BASE}?{query}",
        headers={"Authorization": f"OAuth {(settings.METRICA_API_TOKEN or '').strip()}"},
        method="GET",
    )

    try:
        with request.urlopen(req, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return None

    totals = payload.get("totals")
    if not isinstance(totals, list) or not totals:
        return None

    value = totals[0]
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def get_metrica_visitors_for_period(*, date_from: date, date_to: date) -> int | None:
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    return _request_metrica_users(date_from=date_from, date_to=date_to)


def get_metrica_today_and_all_time_visitors(db: Session, *, now_local: datetime) -> tuple[int | None, int | None]:
    today = now_local.date()

    first_order_at = db.query(func.min(Order.created_at)).scalar()
    if isinstance(first_order_at, datetime) and first_order_at.tzinfo is not None:
        all_time_start = first_order_at.astimezone(now_local.tzinfo).date()
    else:
        all_time_start = today

    today_visitors = get_metrica_visitors_for_period(date_from=today, date_to=today)
    all_time_visitors = get_metrica_visitors_for_period(date_from=all_time_start, date_to=today)
    return today_visitors, all_time_visitors
