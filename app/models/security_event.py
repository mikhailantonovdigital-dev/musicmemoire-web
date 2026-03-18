from __future__ import annotations

from datetime import datetime
from typing import Any, TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

if TYPE_CHECKING:
    from app.models.order import Order


class SecurityEvent(Base):
    __tablename__ = "security_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id"),
        nullable=True,
        index=True,
    )
    action: Mapped[str] = mapped_column(
        String(100),
        index=True,
        nullable=False,
    )
    scope_kind: Mapped[str] = mapped_column(
        String(32),
        index=True,
        nullable=False,
    )
    scope_value: Mapped[str] = mapped_column(
        String(255),
        index=True,
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        index=True,
        nullable=False,
        default="allowed",
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

    order: Mapped["Order | None"] = relationship(back_populates="security_events")
