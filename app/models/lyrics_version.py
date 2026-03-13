from __future__ import annotations

from datetime import datetime
from uuid import uuid4
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

if TYPE_CHECKING:
    from app.models.order import Order


class LyricsVersion(Base):
    __tablename__ = "lyrics_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(36),
        unique=True,
        index=True,
        default=lambda: str(uuid4()),
    )
    order_id: Mapped[int] = mapped_column(
        ForeignKey("orders.id"),
        index=True,
        nullable=False,
    )

    provider: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        index=True,
    )
    model_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    angle_label: Mapped[str] = mapped_column(
        String(120),
        nullable=False,
    )
    prompt_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    lyrics_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    edited_lyrics_text: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    is_selected: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    order: Mapped["Order"] = relationship(back_populates="lyrics_versions")
