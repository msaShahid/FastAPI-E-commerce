import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.shared.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.modules.orders.models.order import Order


class OrderShipment(Base, TimestampMixin):
    """One parcel handed to a carrier for an order. An order can have several (split or re-sent shipments). Tracking details are
    entered by an administrator: NovaCart does not call any courier. `tracking_number` and `tracking_url` are optional because a
    parcel is often handed over before a number exists. `carrier_code` is reserved for a future courier integration (a stable
    machine key such as 'delhivery'); nothing reads it yet."""

    __tablename__ = "order_shipments"
    __table_args__ = (
        CheckConstraint("status IN ('shipped', 'delivered')", name="ck_order_shipments_status"),
        CheckConstraint("(status = 'delivered') = (delivered_at IS NOT NULL)", name="ck_order_shipments_delivered_at"),
        Index("ix_order_shipments_order_id", "order_id"),
        # The same tracking number cannot be recorded twice for one order and carrier (a double entry).
        Index(
            "ux_order_shipments_tracking", "order_id", "carrier_name", "tracking_number", unique=True,
            postgresql_where=text("tracking_number IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    carrier_name: Mapped[str] = mapped_column(String(60))
    carrier_code: Mapped[str | None] = mapped_column(String(30), default=None)
    tracking_number: Mapped[str | None] = mapped_column(String(64), default=None)
    tracking_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    status: Mapped[str] = mapped_column(String(20), default="shipped", server_default="shipped")
    shipped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), default=None)

    order: Mapped["Order"] = relationship(back_populates="shipments")
