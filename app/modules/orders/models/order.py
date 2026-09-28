import uuid
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Enum, ForeignKey, Integer, String, UniqueConstraint
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
        # Scoped per-user, not global: two different users are free to
        UniqueConstraint(
            "user_id", "idempotency_key", name="uq_orders_user_id_idempotency_key"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)

    status: Mapped[OrderStatus] = mapped_column(
        Enum(
            OrderStatus,
            name="order_status",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        default=OrderStatus.PENDING,
        server_default=OrderStatus.PENDING.value,
    )

    # No longer globally unique on its own -- uniqueness is enforced by
    # the composite (user_id, idempotency_key) constraint above.
    idempotency_key: Mapped[str] = mapped_column(String(255), index=True)

    subtotal_cents: Mapped[int] = mapped_column(Integer)
    shipping_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tax_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_cents: Mapped[int] = mapped_column(Integer)

    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    status_history: Mapped[list["OrderStatusHistory"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )