from __future__ import annotations

from datetime import datetime
from uuid import uuid4
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

if TYPE_CHECKING:
    from app.models.order import Order
    from app.models.user import User


class SongGeneration(Base):
    __tablename__ = "song_generations"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(36),
        unique=True,
        index=True,
        default=lambda: str(uuid4()),
    )
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
        index=True,
    )

    provider: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="suno",
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="queued",
        index=True,
    )
    attempt_no: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
    )

    external_job_id: Mapped[str | None] = mapped_column(
        String(120),
        nullable=True,
        index=True,
    )
    lyrics_text_snapshot: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    audio_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        nullable=True,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
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

    order: Mapped["Order"] = relationship(back_populates="song_generations")
    user: Mapped["User | None"] = relationship(back_populates="song_generations")
