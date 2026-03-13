from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone


def utcnow():
    return datetime.now(timezone.utc)


def generate_magic_token() -> str:
    return secrets.token_urlsafe(32)


def hash_magic_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
