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
    from app.models.order_payment import OrderPayment
    from app.models.song_generation import SongGeneration
    from app.models.security_event import SecurityEvent
    from app.models.background_job import BackgroundJob
    from app.models.support_thread import SupportThread
    from app.models.email_log import EmailLog


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
    title: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    song_style: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
    )
    song_style_custom: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    singer_gender: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
    )
    song_mood: Mapped[str | None] = mapped_column(
        String(32),
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
    payments: Mapped[list["OrderPayment"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="OrderPayment.id.desc()",
    )
    song_generations: Mapped[list["SongGeneration"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="SongGeneration.id.desc()",
    )
    security_events: Mapped[list["SecurityEvent"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="SecurityEvent.id.desc()",
    )
    background_jobs: Mapped[list["BackgroundJob"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="BackgroundJob.id.desc()",
    )
    support_threads: Mapped[list["SupportThread"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="SupportThread.id.desc()",
    )
    email_logs: Mapped[list["EmailLog"]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="EmailLog.id.desc()",
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
