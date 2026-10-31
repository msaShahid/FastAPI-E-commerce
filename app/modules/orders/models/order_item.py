from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.shared.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.modules.orders.models.order import Order


class OrderItem(Base, TimestampMixin):

    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[UUID] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)

    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), index=True
    )

    product_name_snapshot: Mapped[str] = mapped_column(String(200))
    price_cents_snapshot: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)

    # What was bought, frozen at purchase (gap E). All NULL on orders that pre-date variants, except sku_snapshot, which the migration
    # filled from the product's SKU (sku_snapshot_source = 'backfill').
    variant_id: Mapped[int | None] = mapped_column(Integer, default=None)
    sku_snapshot: Mapped[str | None] = mapped_column(String(64), default=None)
    sku_snapshot_source: Mapped[str | None] = mapped_column(String(10), default=None)
    variant_snapshot: Mapped[dict | None] = mapped_column(JSONB, default=None)
    # How much of this line has been put back in stock (see order_item_restocks); never more than `quantity`.
    restocked_quantity: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # This line's share of the order's coupon discount (the shares add up to orders.discount_cents). Frozen (database trigger).
    discount_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    order: Mapped["Order"] = relationship(back_populates="items")

    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        CheckConstraint("restocked_quantity >= 0 AND restocked_quantity <= quantity", name="ck_order_items_restocked_within_quantity"),
        CheckConstraint("sku_snapshot_source IS NULL OR sku_snapshot_source IN ('order', 'backfill')", name="ck_order_items_sku_source"),
        CheckConstraint("discount_cents >= 0 AND discount_cents <= price_cents_snapshot * quantity", name="ck_order_items_discount"),
        ForeignKeyConstraint(
            ["variant_id", "product_id"], ["product_variants.id", "product_variants.product_id"],
            name="fk_order_items_variant", ondelete="RESTRICT",
        ),
    )

    @property
    def line_total_cents(self) -> int:

        return self.price_cents_snapshot * self.quantity

from app.modules.orders.models import order_item_restock as _order_item_restock  # noqa: E402,F401
