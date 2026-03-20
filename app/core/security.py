from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from fastapi import Request
from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app.models import User

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def utcnow():
    return datetime.now(timezone.utc)


def generate_magic_token() -> str:
    return secrets.token_urlsafe(32)


def hash_magic_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()




def build_checkout_access_token(payment_public_id: str) -> str:
    from app.core.config import settings

    return hmac.new(
        settings.SESSION_SECRET.encode("utf-8"),
        payment_public_id.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def is_valid_checkout_access_token(payment_public_id: str, token: str | None) -> bool:
    if not token:
        return False
    expected = build_checkout_access_token(payment_public_id)
    return hmac.compare_digest(expected, token.strip())

def normalize_email(value: str) -> str:
    return value.strip().lower()


def is_valid_email(value: str) -> bool:
    return bool(EMAIL_RE.match(value.strip()))


def get_session_user(request: Request, db: Session) -> User | None:
    from app.models import User

    user_id = request.session.get("account_user_id")
    if not user_id:
        return None
    return db.query(User).filter(User.id == int(user_id)).first()
