import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.shared.mixins import TimestampMixin

EVIDENCE_TYPES = ("provider_record", "accounting_record", "store_history_reviewed")
DECISIONS = ("established", "unresolved_conflicting", "unresolved_inconclusive")


class OrderCurrencyRemediation(Base, TimestampMixin):
    """One REVIEW of a legacy order (one with no recorded currency): who reviewed it, when, on what evidence, what was
    decided, and what was done about it. Only an `established` review sets the order's currency (once, immutable
    afterwards by database trigger). Conflicting or inconclusive evidence is recorded too, leaves the currency
    UNRESOLVED and the order flagged for manual review. No amount is ever converted."""

    __tablename__ = "order_currency_remediations"
    __table_args__ = (
        CheckConstraint("currency ~ '^[a-z]{3}$'", name="ck_currency_remediations_currency"),
        CheckConstraint(
            "evidence_type IN ('provider_record', 'accounting_record', 'store_history_reviewed')",
            name="ck_currency_remediations_evidence_type",
        ),
        CheckConstraint("length(btrim(evidence_reference)) > 0", name="ck_currency_remediations_reference"),
        CheckConstraint(
            "decision IN ('established', 'unresolved_conflicting', 'unresolved_inconclusive')",
            name="ck_currency_remediations_decision",
        ),
        # A currency is recorded exactly when the decision is "established".
        CheckConstraint(
            "(decision = 'established' AND currency IS NOT NULL) OR (decision <> 'established' AND currency IS NULL)",
            name="ck_currency_remediations_decision_currency",
        ),
        Index("ux_currency_remediations_one_established", "order_id", unique=True,
              postgresql_where=text("decision = 'established'")),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), index=True)
    currency: Mapped[str | None] = mapped_column(String(3), default=None)  # NULL unless decision = established
    decision: Mapped[str] = mapped_column(String(30), default="established", server_default="established")
    # What was done about it: "currency set to inr" or "left unresolved; manual review required: <why>".
    remediation: Mapped[str | None] = mapped_column(Text, default=None)
    evidence_type: Mapped[str] = mapped_column(String(40))
    evidence_reference: Mapped[str] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    established_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
