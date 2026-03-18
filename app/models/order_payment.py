from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import uuid4
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship

from app.core.config import settings
from app.core.db import Base
from app.core.security import utcnow

if TYPE_CHECKING:
    from app.models.order import Order
    from app.models.user import User

MONEY_QUANT = Decimal('0.01')
REPEAT_ORDER_DISCOUNT_RATE = Decimal('0.50')


def rub_to_amount_value(amount_rub: int | float | Decimal) -> str:
    amount = Decimal(str(amount_rub)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    return f"{amount:.2f}"


def amount_value_to_rub(amount_value: str | None, default: int = 0) -> int:
    if not amount_value:
        return int(default)

    try:
        amount = Decimal(str(amount_value)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return int(default)

    return int(amount)


def build_pricing_snapshot(*, base_price_rub: int, discount_rub: int = 0) -> dict[str, int | str | bool]:
    base_price_rub = max(int(base_price_rub), 0)
    discount_rub = max(int(discount_rub), 0)
    if discount_rub > base_price_rub:
        discount_rub = base_price_rub

    final_price_rub = max(base_price_rub - discount_rub, 0)

    return {
        'base_price_rub': base_price_rub,
        'discount_rub': discount_rub,
        'final_price_rub': final_price_rub,
        'base_amount_value': rub_to_amount_value(base_price_rub),
        'discount_amount_value': rub_to_amount_value(discount_rub),
        'final_amount_value': rub_to_amount_value(final_price_rub),
        'has_discount': discount_rub > 0,
    }


def build_order_pricing_preview(db: Session, order: 'Order') -> dict[str, int | str | bool]:
    base_price_rub = int(settings.PRICE_RUB)

    user_id = order.user_id or getattr(order.user, 'id', None)
    if not user_id:
        return build_pricing_snapshot(base_price_rub=base_price_rub)

    since = utcnow() - timedelta(hours=24)
    recent_success = (
        db.query(OrderPayment)
        .filter(
            OrderPayment.user_id == user_id,
            OrderPayment.order_id != order.id,
            OrderPayment.status == 'succeeded',
            OrderPayment.paid_at.is_not(None),
            OrderPayment.paid_at >= since,
        )
        .order_by(OrderPayment.paid_at.desc(), OrderPayment.id.desc())
        .first()
    )

    if recent_success is None:
        return build_pricing_snapshot(base_price_rub=base_price_rub)

    discount_rub = int(
        (Decimal(str(base_price_rub)) * REPEAT_ORDER_DISCOUNT_RATE).quantize(
            Decimal('1'),
            rounding=ROUND_HALF_UP,
        )
    )
    return build_pricing_snapshot(base_price_rub=base_price_rub, discount_rub=discount_rub)


class OrderPayment(Base):
    __tablename__ = 'order_payments'

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(36),
        unique=True,
        index=True,
        default=lambda: str(uuid4()),
    )
    order_id: Mapped[int] = mapped_column(
        ForeignKey('orders.id'),
        nullable=False,
        index=True,
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey('users.id'),
        nullable=True,
        index=True,
    )

    provider: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default='yookassa',
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default='pending',
        index=True,
    )

    amount_value: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )
    base_amount_value: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )
    discount_amount_value: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )
    final_amount_value: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )
    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default='RUB',
    )

    yookassa_payment_id: Mapped[str | None] = mapped_column(
        String(100),
        unique=True,
        nullable=True,
        index=True,
    )
    idempotence_key: Mapped[str | None] = mapped_column(
        String(64),
        unique=True,
        nullable=True,
        index=True,
    )
    confirmation_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    return_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
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

    order: Mapped['Order'] = relationship(back_populates='payments')
    user: Mapped['User | None'] = relationship(back_populates='payments')

    @property
    def base_amount_rub(self) -> int:
        return amount_value_to_rub(self.base_amount_value, default=self.amount_rub)

    @property
    def discount_amount_rub(self) -> int:
        return amount_value_to_rub(self.discount_amount_value, default=0)

    @property
    def final_amount_rub(self) -> int:
        source = self.final_amount_value or self.amount_value
        return amount_value_to_rub(source, default=self.amount_rub)

    @property
    def amount_rub(self) -> int:
        return amount_value_to_rub(self.amount_value, default=0)

    @property
    def has_discount(self) -> bool:
        return self.discount_amount_rub > 0
