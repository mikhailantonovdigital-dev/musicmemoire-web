from __future__ import annotations

from datetime import datetime
from uuid import uuid4
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base

if TYPE_CHECKING:
    from app.models.order import Order
    from app.models.magic_login_token import MagicLoginToken
    from app.models.order_payment import OrderPayment
    from app.models.song_generation import SongGeneration
    from app.models.support_thread import SupportThread


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(36),
        unique=True,
        index=True,
        default=lambda: str(uuid4()),
    )
    email: Mapped[str | None] = mapped_column(
        String(320),
        unique=True,
        index=True,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    orders: Mapped[list["Order"]] = relationship(back_populates="user")
    magic_login_tokens: Mapped[list["MagicLoginToken"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )
    payments: Mapped[list["OrderPayment"]] = relationship(back_populates="user")
    song_generations: Mapped[list["SongGeneration"]] = relationship(back_populates="user")
    support_threads: Mapped[list["SupportThread"]] = relationship(back_populates="user")
