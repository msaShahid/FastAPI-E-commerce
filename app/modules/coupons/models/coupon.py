import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.shared.mixins import TimestampMixin


class Coupon(Base, TimestampMixin):
    """A discount code a customer types at checkout. One coupon per order. Three kinds:
      percentage     percent_off % of the goods, at most max_discount_cents if that is set
      fixed          amount_off_cents off the goods (never more than the goods cost)
      free_shipping  the shipping charge is waived
    Usage is not stored as a counter: it is counted from the orders that carry the coupon and are still alive (pending, paid or fulfilled),
    so a cancelled, expired or refunded order gives its use back and nothing can drift."""

    __tablename__ = "coupons"
    __table_args__ = (
        UniqueConstraint("code", name="uq_coupons_code"),
        CheckConstraint("code ~ '^[A-Z0-9][A-Z0-9_-]{2,31}$'", name="ck_coupons_code"),
        CheckConstraint("kind IN ('percentage', 'fixed', 'free_shipping')", name="ck_coupons_kind"),
        CheckConstraint(
            "(kind = 'percentage' AND percent_off BETWEEN 0.01 AND 100 AND amount_off_cents IS NULL)"
            " OR (kind = 'fixed' AND amount_off_cents > 0 AND percent_off IS NULL AND max_discount_cents IS NULL)"
            " OR (kind = 'free_shipping' AND percent_off IS NULL AND amount_off_cents IS NULL AND max_discount_cents IS NULL)",
            name="ck_coupons_value",
        ),
        CheckConstraint("max_discount_cents IS NULL OR max_discount_cents > 0", name="ck_coupons_cap"),
        CheckConstraint("min_subtotal_cents >= 0", name="ck_coupons_minimum"),
        CheckConstraint("ends_at IS NULL OR starts_at IS NULL OR ends_at > starts_at", name="ck_coupons_dates"),
        CheckConstraint("max_uses_total IS NULL OR max_uses_total > 0", name="ck_coupons_total_limit"),
        CheckConstraint("max_uses_per_customer IS NULL OR max_uses_per_customer > 0", name="ck_coupons_customer_limit"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32))                      # stored in capitals; customers may type any case
    name: Mapped[str] = mapped_column(String(100))                      # an internal label
    description: Mapped[str | None] = mapped_column(Text, default=None)
    kind: Mapped[str] = mapped_column(String(16))
    percent_off: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), default=None)
    amount_off_cents: Mapped[int | None] = mapped_column(Integer, default=None)
    max_discount_cents: Mapped[int | None] = mapped_column(Integer, default=None)
    min_subtotal_cents: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    max_uses_total: Mapped[int | None] = mapped_column(Integer, default=None)
    max_uses_per_customer: Mapped[int | None] = mapped_column(Integer, default=None)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), default=None)


from app.modules.coupons.models import guard as _guard  # noqa: E402,F401
