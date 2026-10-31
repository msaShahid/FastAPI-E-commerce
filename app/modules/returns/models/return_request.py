import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

STATUSES = ("requested", "approved", "rejected", "received", "closed", "cancelled")
REASONS = ("damaged", "defective", "wrong_item", "not_as_described", "size_or_fit", "changed_mind", "other")
RESOLUTIONS = ("refunded", "refunded_outside", "replacement_sent", "no_refund")


class ReturnRequest(Base):
    """A customer's request to send back goods from a shipped order.

    requested -> approved | rejected | cancelled (by the customer);  approved -> received | closed | cancelled;  received -> closed.
    A refund is NOT part of this record: NovaCart refunds a WHOLE order through the payment provider (a business decision), so a return that
    brings back every unit of an order can start that refund (`refund_id`); any other return is closed with a recorded resolution."""

    __tablename__ = "return_requests"
    __table_args__ = (
        CheckConstraint(f"status IN ({', '.join(repr(s) for s in STATUSES)})", name="ck_return_requests_status"),
        CheckConstraint(f"resolution IS NULL OR resolution IN ({', '.join(repr(s) for s in RESOLUTIONS)})", name="ck_return_requests_resolution"),
        CheckConstraint("status <> 'rejected' OR (rejection_reason IS NOT NULL AND length(btrim(rejection_reason)) >= 3)", name="ck_return_requests_rejection"),
        CheckConstraint("status <> 'closed' OR resolution IS NOT NULL", name="ck_return_requests_closed_resolution"),
        CheckConstraint("customer_comment IS NULL OR length(customer_comment) <= 1000", name="ck_return_requests_comment"),
        Index("ix_return_requests_order", "order_id"),
        Index("ix_return_requests_status_requested", "status", "requested_at"),
        Index("ix_return_requests_user_requested", "user_id", "requested_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    status: Mapped[str] = mapped_column(String(20), default="requested", server_default="requested")
    customer_comment: Mapped[str | None] = mapped_column(Text, default=None)
    admin_note: Mapped[str | None] = mapped_column(Text, default=None)
    rejection_reason: Mapped[str | None] = mapped_column(String(300), default=None)
    resolution: Mapped[str | None] = mapped_column(String(20), default=None)
    resolution_note: Mapped[str | None] = mapped_column(Text, default=None)
    refund_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("refunds.id", ondelete="RESTRICT"), default=None)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    handled_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), default=None)

    items: Mapped[list["ReturnRequestItem"]] = relationship(order_by="ReturnRequestItem.id", lazy="selectin", cascade="all, delete-orphan")


class ReturnRequestItem(Base):
    __tablename__ = "return_request_items"
    __table_args__ = (
        UniqueConstraint("return_id", "order_item_id", name="uq_return_request_items_line"),
        CheckConstraint("quantity > 0", name="ck_return_request_items_quantity"),
        CheckConstraint(f"reason IN ({', '.join(repr(s) for s in REASONS)})", name="ck_return_request_items_reason"),
        CheckConstraint("received_quantity IS NULL OR (received_quantity >= 0 AND received_quantity <= quantity)", name="ck_return_request_items_received"),
        CheckConstraint("restocked_quantity >= 0 AND restocked_quantity <= coalesce(received_quantity, 0)", name="ck_return_request_items_restocked"),
        Index("ix_return_request_items_order_item", "order_item_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    return_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("return_requests.id", ondelete="CASCADE"))
    order_item_id: Mapped[int] = mapped_column(ForeignKey("order_items.id", ondelete="RESTRICT"))
    quantity: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(20))
    received_quantity: Mapped[int | None] = mapped_column(Integer, default=None)
    restock: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    restocked_quantity: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    condition_note: Mapped[str | None] = mapped_column(Text, default=None)
