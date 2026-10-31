"""GST invoices, credit notes and the business profile

Revision ID: f1c4d8e2a937
Revises: e3a7b1c9d452
Create Date: 2026-10-31 14:00:00.000000

Additive: three new tables and one trigger that makes issued invoices immutable. No existing table or row is touched.
The function and trigger text is identical to app/modules/invoices/models/guard.py (a test asserts it).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f1c4d8e2a937"
down_revision: Union[str, Sequence[str], None] = "e3a7b1c9d452"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FUNCTION = r'''CREATE OR REPLACE FUNCTION invoices_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'invoices: an issued invoice or credit note cannot be changed or deleted (issue a credit note)'
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;'''
TRIGGER = "CREATE TRIGGER trg_invoices_immutable BEFORE UPDATE OR DELETE ON invoices FOR EACH ROW EXECUTE FUNCTION invoices_immutable()"


def upgrade() -> None:
    op.create_table(
        "business_profile",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("trade_name", sa.String(length=200), nullable=False),
        sa.Column("address_line1", sa.String(length=255), nullable=False),
        sa.Column("address_line2", sa.String(length=255), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=False),
        sa.Column("state_code", sa.String(length=2), nullable=False),
        sa.Column("pincode", sa.String(length=6), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=30), nullable=True),
        sa.Column("invoice_prefix", sa.String(length=5), server_default="INV", nullable=False),
        sa.Column("footer", sa.Text(), nullable=True),
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_business_profile_single_row"),
        sa.CheckConstraint("pincode ~ '^[0-9]{6}$'", name="ck_business_profile_pincode"),
        sa.CheckConstraint("invoice_prefix ~ '^[A-Z0-9]{1,5}$'", name="ck_business_profile_prefix"),
        sa.CheckConstraint("state_code ~ '^[0-9]{2}$'", name="ck_business_profile_state"),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "invoice_sequences",
        sa.Column("series", sa.String(length=30), nullable=False),
        sa.Column("last_value", sa.Integer(), nullable=False),
        sa.CheckConstraint("last_value >= 0", name="ck_invoice_sequences_value"),
        sa.PrimaryKeyConstraint("series"),
    )
    op.create_table(
        "invoices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("doc_type", sa.String(length=12), nullable=False),
        sa.Column("number", sa.String(length=16), nullable=False),
        sa.Column("fiscal_year", sa.String(length=7), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("issued_on", sa.Date(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("related_invoice_id", sa.Uuid(), nullable=True),
        sa.Column("total_paise", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("document", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint("doc_type IN ('invoice', 'credit_note')", name="ck_invoices_doc_type"),
        sa.CheckConstraint("(doc_type = 'credit_note') = (related_invoice_id IS NOT NULL)", name="ck_invoices_credit_note_reference"),
        sa.CheckConstraint("total_paise >= 0", name="ck_invoices_total"),
        sa.CheckConstraint("length(number) <= 16", name="ck_invoices_number_length"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["related_invoice_id"], ["invoices.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("number", name="uq_invoices_number"),
        sa.UniqueConstraint("order_id", "doc_type", name="uq_invoices_order_doc_type"),
    )
    op.create_index("ix_invoices_issued_on", "invoices", ["issued_on"])
    op.execute(FUNCTION)
    op.execute(TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_invoices_immutable ON invoices")
    op.execute("DROP FUNCTION IF EXISTS invoices_immutable()")
    op.drop_index("ix_invoices_issued_on", table_name="invoices")
    op.drop_table("invoices")
    op.drop_table("invoice_sequences")
    op.drop_table("business_profile")
