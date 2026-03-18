from __future__ import annotations

import hashlib
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
