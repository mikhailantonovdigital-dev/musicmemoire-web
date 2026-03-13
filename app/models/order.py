from __future__ import annotations

from datetime import datetime
from uuid import uuid4
from typing import Any, TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.voice_input import VoiceInput
    from app.models.lyrics_version import LyricsVersion


def generate_order_number() -> str:
    stamp = datetime.utcnow().strftime("%y%m%d")
    suffix = uuid4().hex[:6].upper()
    return f"MM-{stamp}-{suffix}"


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(36),
        unique=True,
        index=True,
        default=lambda: str(uuid4()),
    )
    order_number: Mapped[str] = mapped_column(
        String(32),
        unique=True,
        index=True,
        default=generate_order_number,
    )

    session_id: Mapped[str] = mapped_column(
        String(64),
        index=True,
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(50),
        default="draft",
        index=True,
    )

    story_source: Mapped[str] = mapped_column(
        String(20),
        default="text",
    )
    lyrics_mode: Mapped[str] = mapped_column(
        String(20),
        default="generate",
    )

    story_text: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    transcript_text: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    custom_lyrics_text: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    final_lyrics_text: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    user: Mapped["User | None"] = relationship(back_populates="orders")
    events: Mapped[list["OrderEvent"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
    )
    voice_inputs: Mapped[list["VoiceInput"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
    )
    lyrics_versions: Mapped[list["LyricsVersion"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
    )


class OrderEvent(Base):
    __tablename__ = "order_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id"),
        index=True,
    )
    event_type: Mapped[str] = mapped_column(
        String(100),
        index=True,
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    order: Mapped["Order"] = relationship(back_populates="events")
