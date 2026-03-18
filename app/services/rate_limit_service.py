from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from fastapi import Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.security import utcnow
from app.models import Order, SecurityEvent

COUNTED_SECURITY_STATUSES = ("allowed", "blocked", "suspicious")


@dataclass(slots=True)
class RateLimitRule:
    scope_kind: str
    scope_value: str | int | None
    limit: int
    window_seconds: int


@dataclass(slots=True)
class RateLimitDecision:
    allowed: bool
    message: str | None = None
    suspicious: bool = False
    triggered: list[dict[str, Any]] | None = None


def get_client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").strip()
    if forwarded:
        first = forwarded.split(",", 1)[0].strip()
        if first:
            return first[:255]

    for header_name in ("cf-connecting-ip", "x-real-ip"):
        value = (request.headers.get(header_name) or "").strip()
        if value:
            return value[:255]

    client_host = getattr(getattr(request, "client", None), "host", None)
    return (client_host or "unknown")[:255]


def _clean_scope_value(value: str | int | None) -> str:
    if value is None:
        return ""
    return str(value).strip()[:255]


def _build_payload(request: Request, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    merged: dict[str, Any] = dict(payload or {})
    merged.setdefault("path", request.url.path)
    merged.setdefault("method", request.method)
    merged.setdefault("ip", get_client_ip(request))
    user_agent = (request.headers.get("user-agent") or "").strip()
    if user_agent:
        merged.setdefault("user_agent", user_agent[:500])
    referer = (request.headers.get("referer") or "").strip()
    if referer:
        merged.setdefault("referer", referer[:500])
    return merged


def record_security_event(
    db: Session,
    *,
    action: str,
    scope_kind: str,
    scope_value: str | int | None,
    status: str,
    request: Request,
    order: Order | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    cleaned_scope = _clean_scope_value(scope_value)
    if not cleaned_scope:
        return

    db.add(
        SecurityEvent(
            order_id=order.id if order else None,
            action=action,
            scope_kind=scope_kind,
            scope_value=cleaned_scope,
            status=status,
            payload=_build_payload(request, payload),
        )
    )


def count_recent_security_events(
    db: Session,
    *,
    action: str,
    scope_kind: str,
    scope_value: str | int | None,
    window_seconds: int,
) -> int:
    cleaned_scope = _clean_scope_value(scope_value)
    if not cleaned_scope:
        return 0

    since = utcnow() - timedelta(seconds=window_seconds)
    return (
        db.query(func.count(SecurityEvent.id))
        .filter(
            SecurityEvent.action == action,
            SecurityEvent.scope_kind == scope_kind,
            SecurityEvent.scope_value == cleaned_scope,
            SecurityEvent.status.in_(COUNTED_SECURITY_STATUSES),
            SecurityEvent.created_at >= since,
        )
        .scalar()
        or 0
    )


def enforce_rate_limit(
    db: Session,
    *,
    request: Request,
    action: str,
    user_message: str,
    rules: list[RateLimitRule],
    order: Order | None = None,
    extra_payload: dict[str, Any] | None = None,
) -> RateLimitDecision:
    triggered: list[dict[str, Any]] = []
    normalized_rules: list[tuple[RateLimitRule, str]] = []

    for rule in rules:
        cleaned_scope = _clean_scope_value(rule.scope_value)
        if not cleaned_scope:
            continue
        normalized_rules.append((rule, cleaned_scope))
        recent_count = count_recent_security_events(
            db,
            action=action,
            scope_kind=rule.scope_kind,
            scope_value=cleaned_scope,
            window_seconds=rule.window_seconds,
        )
        if recent_count >= rule.limit:
            triggered.append(
                {
                    "scope_kind": rule.scope_kind,
                    "scope_value": cleaned_scope,
                    "window_seconds": rule.window_seconds,
                    "limit": rule.limit,
                    "recent_count": recent_count,
                }
            )

    if triggered:
        suspicious = any(item["recent_count"] >= max(item["limit"] * 2, item["limit"] + 3) for item in triggered)
        status = "suspicious" if suspicious else "blocked"
        for item in triggered:
            record_security_event(
                db,
                action=action,
                scope_kind=item["scope_kind"],
                scope_value=item["scope_value"],
                status=status,
                request=request,
                order=order,
                payload={
                    **(extra_payload or {}),
                    "limit": item["limit"],
                    "window_seconds": item["window_seconds"],
                    "recent_count": item["recent_count"],
                    "triggered": triggered,
                },
            )

        return RateLimitDecision(
            allowed=False,
            message=user_message,
            suspicious=suspicious,
            triggered=triggered,
        )

    for rule, cleaned_scope in normalized_rules:
        record_security_event(
            db,
            action=action,
            scope_kind=rule.scope_kind,
            scope_value=cleaned_scope,
            status="allowed",
            request=request,
            order=order,
            payload={
                **(extra_payload or {}),
                "limit": rule.limit,
                "window_seconds": rule.window_seconds,
            },
        )

    return RateLimitDecision(allowed=True)
