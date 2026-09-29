import uuid
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import CheckConstraint, Enum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.shared.enums.order_status import OrderStatus
from app.shared.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.modules.orders.models.order_item import OrderItem
    from app.modules.orders.models.order_status_history import OrderStatusHistory


class Order(Base, TimestampMixin):

    __tablename__ = "orders"
    __table_args__ = (

        UniqueConstraint(
            "user_id", "idempotency_key", name="uq_orders_user_id_idempotency_key"
        ),
        UniqueConstraint(
            "guest_token", "idempotency_key", name="uq_orders_guest_token_idempotency_key"
        ),
        CheckConstraint(
            "(user_id IS NOT NULL AND guest_token IS NULL AND guest_email IS NULL) OR "
            "(user_id IS NULL AND guest_token IS NOT NULL AND guest_email IS NOT NULL)",
            name="ck_orders_user_xor_guest",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), index=True, default=None
    )

    guest_token: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    guest_email: Mapped[str | None] = mapped_column(String(255), default=None)

    status: Mapped[OrderStatus] = mapped_column(
        Enum(
            OrderStatus,
            name="order_status",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        default=OrderStatus.PENDING,
        server_default=OrderStatus.PENDING.value,
    )

    idempotency_key: Mapped[str] = mapped_column(String(255), index=True)

    subtotal_cents: Mapped[int] = mapped_column(Integer)
    shipping_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tax_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_cents: Mapped[int] = mapped_column(Integer)
    shipping_recipient_name: Mapped[str | None] = mapped_column(String(200), default=None)
    shipping_line1: Mapped[str | None] = mapped_column(String(255), default=None)
    shipping_line2: Mapped[str | None] = mapped_column(String(255), default=None)
    shipping_city: Mapped[str | None] = mapped_column(String(100), default=None)
    shipping_state: Mapped[str | None] = mapped_column(String(100), default=None)
    shipping_postal_code: Mapped[str | None] = mapped_column(String(20), default=None)
    shipping_country: Mapped[str | None] = mapped_column(String(2), default=None)

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    status_history: Mapped[list["OrderStatusHistory"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )