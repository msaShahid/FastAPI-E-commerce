import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class StockMovement(Base):
    """The stock ledger: one row for EVERY change of a product's or variant's stock, with before and after values.

    Written only by database triggers, in the same transaction as the stock change, so a stock change and its record commit or roll
    back together and no code path can change stock unrecorded. Append-only: the database refuses UPDATE, DELETE and any INSERT not made
    by the stock triggers. (A database OWNER can still disable triggers or TRUNCATE: that is a deployment permission, see docs.)"""

    __tablename__ = "stock_movements"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('opening', 'initial', 'sale', 'restock', 'admin_set', 'admin_delta', 'conversion', 'unspecified')",
            name="ck_stock_movements_kind",
        ),
        CheckConstraint("delta <> 0", name="ck_stock_movements_delta"),
        CheckConstraint("after_qty = before_qty + delta AND after_qty >= 0 AND before_qty >= 0", name="ck_stock_movements_balance"),
        # An administrator's change always says who did it and why.
        CheckConstraint(
            "kind NOT IN ('admin_set', 'admin_delta') OR (actor_user_id IS NOT NULL AND length(btrim(reason)) >= 3)",
            name="ck_stock_movements_admin_reason",
        ),
        Index("ix_stock_movements_product_id", "product_id", "id"),
        Index("ix_stock_movements_variant_id", "variant_id", "id", postgresql_where=text("variant_id IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    kind: Mapped[str] = mapped_column(String(20))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    variant_id: Mapped[int | None] = mapped_column(ForeignKey("product_variants.id", ondelete="RESTRICT"), default=None)
    delta: Mapped[int] = mapped_column(Integer)
    before_qty: Mapped[int] = mapped_column(Integer)
    after_qty: Mapped[int] = mapped_column(Integer)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="NO ACTION"), default=None)
    reason: Mapped[str | None] = mapped_column(String(200), default=None)
    order_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
