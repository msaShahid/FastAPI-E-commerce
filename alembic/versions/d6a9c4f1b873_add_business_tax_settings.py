"""add business_tax_settings (the business's approved GST configuration)

Revision ID: d6a9c4f1b873
Revises: c3d8a2e6f914
Create Date: 2026-10-17 21:30:00.000000

Additive: one new EMPTY table. Nothing is seeded, no existing table changes. Each deployment
(one per client) fills it through the admin API; no client-specific value lives in code.
Reuses the existing tax_master_guard() trigger function (created by revision f2b6d8a4c917)
so approved records cannot be rewritten or deleted by any code path.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d6a9c4f1b873"
down_revision: Union[str, Sequence[str], None] = "c3d8a2e6f914"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "business_tax_settings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pricing_mode", sa.String(length=10), nullable=False),
        sa.Column("place_of_supply_basis", sa.String(length=20), nullable=False),
        sa.Column("shipping_tax_treatment", sa.String(length=30), nullable=False),
        sa.Column("rounding_policy", sa.String(length=20), nullable=False),
        sa.Column("registration_id", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="draft", nullable=False),
        sa.Column("source_reference", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("approved_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["registration_id"], ["gst_registrations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["approved_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("status IN ('draft', 'approved', 'retired')", name="ck_business_tax_settings_status"),
        sa.CheckConstraint("pricing_mode IN ('inclusive', 'exclusive')", name="ck_business_tax_settings_pricing"),
        sa.CheckConstraint("place_of_supply_basis IN ('delivery_state')", name="ck_business_tax_settings_pos"),
        sa.CheckConstraint(
            "shipping_tax_treatment IN ('not_taxed', 'taxable_with_goods')",
            name="ck_business_tax_settings_shipping",
        ),
        sa.CheckConstraint(
            "rounding_policy IN ('line_half_up', 'invoice_half_up')", name="ck_business_tax_settings_rounding"
        ),
        sa.CheckConstraint(
            "effective_to IS NULL OR effective_to > effective_from", name="ck_business_tax_settings_dates"
        ),
        sa.CheckConstraint(
            "status <> 'approved' OR (approved_at IS NOT NULL AND "
            "source_reference IS NOT NULL AND length(btrim(source_reference)) > 0)",
            name="ck_business_tax_settings_approved_fields",
        ),
    )
    op.create_index(op.f("ix_business_tax_settings_registration_id"), "business_tax_settings", ["registration_id"])
    op.execute(
        "ALTER TABLE business_tax_settings ADD CONSTRAINT ex_business_tax_settings_no_approved_overlap "
        "EXCLUDE USING gist (daterange(effective_from, effective_to, '[)') WITH &&) WHERE (status = 'approved')"
    )
    op.execute(
        "CREATE TRIGGER trg_business_tax_settings_guard BEFORE INSERT OR UPDATE OR DELETE "
        "ON business_tax_settings FOR EACH ROW EXECUTE FUNCTION tax_master_guard()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_business_tax_settings_guard ON business_tax_settings")
    op.drop_index(op.f("ix_business_tax_settings_registration_id"), table_name="business_tax_settings")
    op.drop_table("business_tax_settings")
