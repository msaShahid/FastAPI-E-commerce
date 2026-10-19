import uuid
from datetime import datetime

from sqlalchemy import DDL, CheckConstraint, ForeignKey, Integer, String, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.modules.payments.models.provider_guard import PROVIDER_FUNCTIONS, PROVIDER_TRIGGERS
from app.modules.payments.models.refund import Refund
from app.shared.mixins import TimestampMixin


class Dispute(Base, TimestampMixin):
    """A payment dispute (chargeback) reported by the provider. Recording it changes NO order, stock or
    refund by itself: each transition is audited and a reconciliation record asks a human to respond.
    While one is open, refunding that payment is refused (refunding a disputed charge risks paying twice)."""

    __tablename__ = "disputes"
    __table_args__ = (
        UniqueConstraint("provider", "provider_dispute_id", name="uq_disputes_provider_id"),
        CheckConstraint("status IN ('open', 'under_review', 'won', 'lost', 'closed')", name="ck_disputes_status"),
        CheckConstraint("currency ~ '^[a-z]{3}$'", name="ck_disputes_currency_format"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(30))
    provider_dispute_id: Mapped[str] = mapped_column(String(255))
    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id", ondelete="RESTRICT"), index=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(20), default="open", server_default="open", index=True)
    provider_status: Mapped[str | None] = mapped_column(String(50), default=None)  # as the provider said it
    reason: Mapped[str | None] = mapped_column(String(100), default=None)
    closed_at: Mapped[datetime | None] = mapped_column(default=None)


for _function_sql in PROVIDER_FUNCTIONS:
    event.listen(
        Base.metadata,
        "before_create",
        DDL(_function_sql.replace("%", "%%")).execute_if(dialect="postgresql"),
    )
_TABLES = {"refunds": Refund.__table__, "disputes": Dispute.__table__}
for _name, (_table, _when, _function) in PROVIDER_TRIGGERS.items():
    event.listen(
        _TABLES[_table],
        "after_create",
        DDL(f"CREATE TRIGGER {_name} {_when} ON {_table} FOR EACH ROW EXECUTE FUNCTION {_function}()").execute_if(
            dialect="postgresql"
        ),
    )
