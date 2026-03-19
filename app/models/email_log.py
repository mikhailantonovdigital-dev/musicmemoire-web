from __future__ import annotations

from datetime import datetime
from uuid import uuid4
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

if TYPE_CHECKING:
    from app.models.order import Order
    from app.models.user import User


class EmailLog(Base):
    __tablename__ = "email_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(36),
        unique=True,
        index=True,
        default=lambda: str(uuid4()),
    )
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id"),
        nullable=True,
        index=True,
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"),
        nullable=True,
        index=True,
    )
    background_job_public_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
        index=True,
    )
    email_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        index=True,
    )
    recipient_email: Mapped[str] = mapped_column(
        String(320),
        nullable=False,
        index=True,
    )
    subject: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )
    delivery_mode: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="email",
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="sent",
        index=True,
    )
    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
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
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    order: Mapped["Order | None"] = relationship(back_populates="email_logs")
    user: Mapped["User | None"] = relationship(back_populates="email_logs")
