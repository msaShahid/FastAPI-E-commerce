import uuid
from datetime import datetime

from sqlalchemy import DDL, CheckConstraint, ForeignKey, Index, Integer, String, Text, event, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.modules.orders.models.order import Order
from app.modules.payments.models.lifecycle_guard import (
    LIFECYCLE_FUNCTIONS,
    LIFECYCLE_TRIGGERS,
)
from app.shared.mixins import TimestampMixin


class Refund(Base, TimestampMixin):
    """A refund of one payment, tracked from REQUEST to provider-CONFIRMED outcome.

    The order only becomes `refunded` after a refund here reaches `succeeded`, a status set
    solely from a provider-verified result. One active refund per payment (unique index).
    """

    __tablename__ = "refunds"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_refunds_amount_positive"),
        CheckConstraint("currency ~ '^[a-z]{3}$'", name="ck_refunds_currency_format"),
        CheckConstraint(
            "status IN ('requested', 'pending', 'succeeded', 'failed', 'uncertain')", name="ck_refunds_status"
        ),
        Index(
            "ux_refunds_one_active_per_payment",
            "payment_id",
            unique=True,
            postgresql_where=text("status IN ('requested', 'pending', 'succeeded', 'uncertain')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id", ondelete="RESTRICT"), index=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(20), default="requested", server_default="requested")
    provider_refund_id: Mapped[str | None] = mapped_column(String(255), default=None)
    # The provider-side idempotency key: retrying can never create a second refund at the provider.
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True)
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    failure_reason: Mapped[str | None] = mapped_column(Text, default=None)


class PaymentReconciliation(Base, TimestampMixin):
    """Something about money that a human must look at. Created by the system, resolved by an
    administrator with a note. Nothing here moves money or changes an order by itself."""

    __tablename__ = "payment_reconciliations"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'resolved')", name="ck_reconciliations_status"),
        CheckConstraint(
            "status = 'open' OR (resolved_at IS NOT NULL AND resolution IS NOT NULL "
            "AND resolution_note IS NOT NULL AND length(btrim(resolution_note)) > 0)",
            name="ck_reconciliations_resolved_fields",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(20), default="open", server_default="open", index=True)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payments.id", ondelete="SET NULL"), default=None)
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), default=None)
    provider_payment_id: Mapped[str | None] = mapped_column(String(255), default=None)
    details: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    # The same problem is recorded once, however many times the event is redelivered.
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True)
    resolution: Mapped[str | None] = mapped_column(String(40), default=None)
    resolution_note: Mapped[str | None] = mapped_column(Text, default=None)
    resolved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    resolved_at: Mapped[datetime | None] = mapped_column(default=None)


for _function_sql in LIFECYCLE_FUNCTIONS:
    event.listen(
        Base.metadata,
        "before_create",
        DDL(_function_sql.replace("%", "%%")).execute_if(dialect="postgresql"),
    )
_TABLES = {"orders": Order.__table__, "refunds": Refund.__table__}
for _name, (_table, _when, _function) in LIFECYCLE_TRIGGERS.items():
    event.listen(
        _TABLES[_table],
        "after_create",
        DDL(f"CREATE TRIGGER {_name} {_when} ON {_table} FOR EACH ROW EXECUTE FUNCTION {_function}()").execute_if(
            dialect="postgresql"
        ),
    )
