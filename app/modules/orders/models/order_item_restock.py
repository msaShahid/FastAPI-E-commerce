from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class OrderItemRestock(Base):
    """One idempotent restock event for one order line. (order_item_id, event_key) is unique, so replaying an event is a no-op;
    a trigger adds the quantity to order_items.restocked_quantity, whose CHECK keeps it within the purchased quantity. This is the
    per-line boundary for restocking: the order-level stock_released_at flag is only a summary of it."""

    __tablename__ = "order_item_restocks"
    __table_args__ = (
        UniqueConstraint("order_item_id", "event_key", name="uq_order_item_restocks_event"),
        CheckConstraint("quantity > 0", name="ck_order_item_restocks_quantity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_item_id: Mapped[int] = mapped_column(ForeignKey("order_items.id", ondelete="CASCADE"))
    event_key: Mapped[str] = mapped_column(String(100))
    quantity: Mapped[int] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
