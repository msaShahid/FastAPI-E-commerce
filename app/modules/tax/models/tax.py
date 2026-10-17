"""
GST master data for ONE business: approved HSN/SAC classifications, effective-dated
tax rules, and the business's GST registrations. Nothing here is seeded and nothing
here is used by checkout yet. Records start as `draft`; only an approved record may
ever be used, and approved records are never edited in place (they are closed or
retired, and replaced by a new record).
"""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    DDL,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.shared.mixins import TimestampMixin

# The overlap exclusion constraint needs this extension (test schemas are built from models).
event.listen(Base.metadata, "before_create", DDL("CREATE EXTENSION IF NOT EXISTS btree_gist"))

_STATUS_OK = "status IN ('draft', 'approved', 'retired')"


class _Approval:
    """Who created / approved a record, and when."""

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class TaxClassification(Base, TimestampMixin, _Approval):
    __tablename__ = "tax_classifications"
    __table_args__ = (
        UniqueConstraint("kind", "code", name="uq_tax_classifications_kind_code"),
        CheckConstraint("kind IN ('hsn', 'sac')", name="ck_tax_classifications_kind"),
        CheckConstraint("code ~ '^[0-9]+$'", name="ck_tax_classifications_code_digits"),
        CheckConstraint(_STATUS_OK, name="ck_tax_classifications_status"),
        CheckConstraint(
            "status <> 'approved' OR (approved_at IS NOT NULL AND "
            "source_reference IS NOT NULL AND length(btrim(source_reference)) > 0)",
            name="ck_tax_classifications_approved_fields",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(3))
    code: Mapped[str] = mapped_column(String(8), index=True)
    description: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="draft", server_default="draft")
    # Where the classification was verified (official notification / professional's sign-off).
    source_reference: Mapped[str | None] = mapped_column(Text, default=None)


class TaxRule(Base, TimestampMixin, _Approval):
    __tablename__ = "tax_rules"
    __table_args__ = (
        CheckConstraint(_STATUS_OK, name="ck_tax_rules_status"),
        CheckConstraint(
            "treatment IN ('taxable', 'exempt', 'nil_rated', 'non_gst')",
            name="ck_tax_rules_treatment",
        ),
        CheckConstraint(
            "cgst_rate BETWEEN 0 AND 100 AND sgst_utgst_rate BETWEEN 0 AND 100 "
            "AND igst_rate BETWEEN 0 AND 100",
            name="ck_tax_rules_rate_range",
        ),
        # A taxable rule must carry real rates that add up; any other treatment
        # is explicit and carries no rate. Zero is never an implicit default.
        CheckConstraint(
            "(treatment = 'taxable' AND igst_rate > 0 AND cgst_rate + sgst_utgst_rate = igst_rate) "
            "OR (treatment <> 'taxable' AND cgst_rate = 0 AND sgst_utgst_rate = 0 AND igst_rate = 0)",
            name="ck_tax_rules_treatment_rates",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from", name="ck_tax_rules_dates"
        ),
        CheckConstraint(
            "status <> 'approved' OR (approved_at IS NOT NULL AND "
            "source_reference IS NOT NULL AND length(btrim(source_reference)) > 0)",
            name="ck_tax_rules_approved_fields",
        ),
        # Two approved rules for one classification can never apply on the same day.
        ExcludeConstraint(
            ("classification_id", "="),
            (text("daterange(effective_from, effective_to, '[)')"), "&&"),
            using="gist",
            where=text("status = 'approved'"),
            name="ex_tax_rules_no_approved_overlap",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    classification_id: Mapped[int] = mapped_column(
        ForeignKey("tax_classifications.id", ondelete="RESTRICT"), index=True
    )
    treatment: Mapped[str] = mapped_column(String(20))
    cgst_rate: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=Decimal("0"))
    sgst_utgst_rate: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=Decimal("0"))
    igst_rate: Mapped[Decimal] = mapped_column(Numeric(6, 3), default=Decimal("0"))
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date, default=None)  # exclusive end
    status: Mapped[str] = mapped_column(String(20), default="draft", server_default="draft")
    # Official notification / approval reference. Required to approve.
    source_reference: Mapped[str | None] = mapped_column(Text, default=None)


class GstRegistration(Base, TimestampMixin, _Approval):
    """One GST registration of the business. Selecting which one applies is NOT implemented."""

    __tablename__ = "gst_registrations"
    __table_args__ = (
        CheckConstraint(_STATUS_OK, name="ck_gst_registrations_status"),
        CheckConstraint(
            "gstin ~ '^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$'",
            name="ck_gst_registrations_gstin_format",
        ),
        CheckConstraint("state_code ~ '^[0-9]{2}$'", name="ck_gst_registrations_state_code"),
        CheckConstraint("substr(gstin, 1, 2) = state_code", name="ck_gst_registrations_gstin_state"),
        CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from", name="ck_gst_registrations_dates"
        ),
        CheckConstraint(
            "status <> 'approved' OR approved_at IS NOT NULL",
            name="ck_gst_registrations_approved_fields",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    gstin: Mapped[str] = mapped_column(String(15), unique=True)
    legal_name: Mapped[str] = mapped_column(String(200))
    state_code: Mapped[str] = mapped_column(String(2))
    valid_from: Mapped[date] = mapped_column(Date)
    valid_to: Mapped[date | None] = mapped_column(Date, default=None)
    status: Mapped[str] = mapped_column(String(20), default="draft", server_default="draft")
