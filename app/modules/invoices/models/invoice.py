import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class InvoiceSequence(Base):
    """The last number used in a series (one series per document type per financial year). Incremented by an upsert in the SAME
    transaction that stores the document, so numbers are consecutive: a rolled-back document gives its number back."""

    __tablename__ = "invoice_sequences"
    __table_args__ = (CheckConstraint("last_value >= 0", name="ck_invoice_sequences_value"),)

    series: Mapped[str] = mapped_column(String(30), primary_key=True)
    last_value: Mapped[int] = mapped_column(Integer, default=0)


class Invoice(Base):
    """A tax invoice or a credit note, frozen at issue (`document` holds everything printed on it). Never updated or deleted: the
    database refuses both. A mistake is corrected by a credit note, as the GST rules require."""

    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("number", name="uq_invoices_number"),
        UniqueConstraint("order_id", "doc_type", name="uq_invoices_order_doc_type"),
        CheckConstraint("doc_type IN ('invoice', 'credit_note')", name="ck_invoices_doc_type"),
        CheckConstraint("(doc_type = 'credit_note') = (related_invoice_id IS NOT NULL)", name="ck_invoices_credit_note_reference"),
        CheckConstraint("total_paise >= 0", name="ck_invoices_total"),
        CheckConstraint("length(number) <= 16", name="ck_invoices_number_length"),
        Index("ix_invoices_issued_on", "issued_on"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    doc_type: Mapped[str] = mapped_column(String(12))
    number: Mapped[str] = mapped_column(String(16))
    fiscal_year: Mapped[str] = mapped_column(String(7))
    sequence: Mapped[int] = mapped_column(Integer)
    issued_on: Mapped[date] = mapped_column(Date)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    related_invoice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("invoices.id", ondelete="RESTRICT"), default=None)
    total_paise: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    document: Mapped[dict] = mapped_column(JSONB)


from app.modules.invoices.models import guard as _guard  # noqa: E402,F401
