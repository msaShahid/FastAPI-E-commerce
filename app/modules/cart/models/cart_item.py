from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, Integer, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.shared.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.modules.cart.models.cart import Cart
    from app.modules.products.models.product import Product


class CartItem(Base, TimestampMixin):
    __tablename__ = "cart_items"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    cart_id: Mapped[int] = mapped_column(ForeignKey("carts.id", ondelete="CASCADE"), index=True)

    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), index=True
    )

    # NULL for a simple product; the chosen variant for a product with options (the pair is checked by a composite foreign key).
    variant_id: Mapped[int | None] = mapped_column(Integer, default=None)

    quantity: Mapped[int] = mapped_column(Integer)
    price_cents_snapshot: Mapped[int] = mapped_column(Integer)

    cart: Mapped["Cart"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship()

    __table_args__ = (
        ForeignKeyConstraint(
            ["variant_id", "product_id"], ["product_variants.id", "product_variants.product_id"],
            name="fk_cart_items_variant", ondelete="RESTRICT",
        ),
        # One line per product (simple products) or per variant: the same rule as before, split by line kind.
        Index("ux_cart_items_cart_product_simple", "cart_id", "product_id", unique=True, postgresql_where=text("variant_id IS NULL")),
        Index("ux_cart_items_cart_variant", "cart_id", "variant_id", unique=True, postgresql_where=text("variant_id IS NOT NULL")),
        CheckConstraint("quantity > 0", name="ck_cart_items_quantity_positive"),
    )