import uuid
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
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
        CheckConstraint("currency IS NULL OR currency ~ '^[a-z]{3}$'", name="ck_orders_currency_format"),
        CheckConstraint("discount_cents >= 0 AND discount_cents <= subtotal_cents", name="ck_orders_discount_within_subtotal"),
        Index("ix_orders_coupon_id", "coupon_id", postgresql_where=text("coupon_id IS NOT NULL")),
        CheckConstraint("(coupon_id IS NULL) = (coupon_code IS NULL) AND (coupon_id IS NOT NULL OR discount_cents = 0)", name="ck_orders_coupon_consistent"),
        CheckConstraint(
            "(user_id IS NOT NULL AND guest_token IS NULL AND guest_email IS NULL) OR "
            "(user_id IS NULL AND guest_token IS NOT NULL AND guest_email IS NOT NULL)",
            name="ck_orders_user_xor_guest",
        ),
        # "My orders": WHERE user_id = ? ORDER BY created_at DESC. Starts with
        # user_id, so it replaces the old single-column user_id index.
        Index("ix_orders_user_id_created_at", "user_id", "created_at"),
        # Admin "all orders", newest first.
        Index("ix_orders_created_at", "created_at"),
        # Admin filtered by status, and the stale-pending-order reaper
        # (status = 'pending' AND created_at < cutoff).
        Index("ix_orders_status_created_at", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), default=None
    )

    guest_token: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    guest_email: Mapped[str | None] = mapped_column(String(255), default=None)
    # SHA-256 of the order-scoped guest PAYMENT capability (a random 256-bit secret given to the guest once, in an HttpOnly
    # cookie). Only the hash is stored. NULL for registered-user orders and for guest orders made before this existed.
    payment_token_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    # Hash of the cart lines this order was made from (product, variant, quantity). Lets a replayed idempotency key be refused when it
    # arrives with a DIFFERENT cart. NULL on orders that pre-date it: those compare on the address alone, as before.
    cart_digest: Mapped[str | None] = mapped_column(String(64), default=None)

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
    # SHA-256 of the checkout request (address + guest email) that created this
    # order. A replay of the same key with a different request is rejected.
    # NULL on orders created before this column existed (replays are then
    # accepted as before, since there is nothing to compare against).
    request_hash: Mapped[str | None] = mapped_column(String(64), default=None)

    subtotal_cents: Mapped[int] = mapped_column(Integer)
    # What a coupon took off the GOODS (total = subtotal - discount + shipping + tax). Frozen with the order (database trigger).
    discount_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    coupon_id: Mapped[int | None] = mapped_column(ForeignKey("coupons.id", ondelete="RESTRICT"), default=None)
    coupon_code: Mapped[str | None] = mapped_column(String(32), default=None)
    coupon_snapshot: Mapped[dict | None] = mapped_column(JSONB, default=None)  # the coupon's terms and what it did, as they were
    shipping_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    tax_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_cents: Mapped[int] = mapped_column(Integer)
    # How shipping_cents was decided (policy values in force at checkout). Frozen
    # with the order; later settings changes never alter it. NULL on legacy orders.
    # ISO 4217 code (lower case) of every *_cents amount on this order. Copied from the store
    # currency when the order is created and immutable afterwards (database trigger). NULL on
    # orders created before currencies were recorded: their currency is unknown, not assumed.
    currency: Mapped[str | None] = mapped_column(String(3), default=None)
    # When this order's reserved stock was put back. Set exactly once, in the same transaction as
    # the cancellation (database trigger: cannot be repeated, undone, or skipped), so stock can
    # never be restocked twice. NULL for orders that never released stock.
    stock_released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    shipping_snapshot: Mapped[dict | None] = mapped_column(JSONB, default=None)
    # How tax_cents was decided: which engine, its inputs/outputs, and whether it is a GST
    # calculation (`gst_calculated`). Frozen with the order. NULL on legacy orders.
    tax_snapshot: Mapped[dict | None] = mapped_column(JSONB, default=None)
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
    # Parcels handed to carriers (entered by an administrator). Never cascade-deleted: shipment records outlive mistakes.
    shipments: Mapped[list["OrderShipment"]] = relationship(back_populates="order", order_by="OrderShipment.created_at")

# Registers the OrderShipment mapper whenever Order is imported (the relationship above names it).
from app.modules.shipments.models.order_shipment import OrderShipment  # noqa: E402,F401
