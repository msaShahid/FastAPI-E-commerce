"""add GST master tables (classifications, rules, business registrations)

Revision ID: e7a3c9b1d845
Revises: d5f1b2c9a604
Create Date: 2026-10-17 17:30:00.000000

Additive only. Creates three empty tables; nothing is seeded and no existing table
is changed. Rules overlap protection uses an exclusion constraint, which needs the
btree_gist extension (a trusted extension, creatable by the database owner).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e7a3c9b1d845"
down_revision: Union[str, Sequence[str], None] = "d5f1b2c9a604"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STATUS_OK = "status IN ('draft', 'approved', 'retired')"


def _approval_columns() -> list[sa.Column]:
    return [
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("approved_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["approved_by_user_id"], ["users.id"], ondelete="SET NULL"),
    ]


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    op.create_table(
        "tax_classifications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=3), nullable=False),
        sa.Column("code", sa.String(length=8), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="draft", nullable=False),
        sa.Column("source_reference", sa.Text(), nullable=True),
        *_approval_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "code", name="uq_tax_classifications_kind_code"),
        sa.CheckConstraint("kind IN ('hsn', 'sac')", name="ck_tax_classifications_kind"),
        sa.CheckConstraint("code ~ '^[0-9]+$'", name="ck_tax_classifications_code_digits"),
        sa.CheckConstraint(STATUS_OK, name="ck_tax_classifications_status"),
        sa.CheckConstraint(
            "status <> 'approved' OR (approved_at IS NOT NULL AND "
            "source_reference IS NOT NULL AND length(btrim(source_reference)) > 0)",
            name="ck_tax_classifications_approved_fields",
        ),
    )
    op.create_index(op.f("ix_tax_classifications_code"), "tax_classifications", ["code"])

    op.create_table(
        "tax_rules",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("classification_id", sa.Integer(), nullable=False),
        sa.Column("treatment", sa.String(length=20), nullable=False),
        sa.Column("cgst_rate", sa.Numeric(6, 3), nullable=False),
        sa.Column("sgst_utgst_rate", sa.Numeric(6, 3), nullable=False),
        sa.Column("igst_rate", sa.Numeric(6, 3), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="draft", nullable=False),
        sa.Column("source_reference", sa.Text(), nullable=True),
        *_approval_columns(),
        sa.ForeignKeyConstraint(["classification_id"], ["tax_classifications.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(STATUS_OK, name="ck_tax_rules_status"),
        sa.CheckConstraint(
            "treatment IN ('taxable', 'exempt', 'nil_rated', 'non_gst')", name="ck_tax_rules_treatment"
        ),
        sa.CheckConstraint(
            "cgst_rate BETWEEN 0 AND 100 AND sgst_utgst_rate BETWEEN 0 AND 100 "
            "AND igst_rate BETWEEN 0 AND 100",
            name="ck_tax_rules_rate_range",
        ),
        sa.CheckConstraint(
            "(treatment = 'taxable' AND igst_rate > 0 AND cgst_rate + sgst_utgst_rate = igst_rate) "
            "OR (treatment <> 'taxable' AND cgst_rate = 0 AND sgst_utgst_rate = 0 AND igst_rate = 0)",
            name="ck_tax_rules_treatment_rates",
        ),
        sa.CheckConstraint("effective_to IS NULL OR effective_to > effective_from", name="ck_tax_rules_dates"),
        sa.CheckConstraint(
            "status <> 'approved' OR (approved_at IS NOT NULL AND "
            "source_reference IS NOT NULL AND length(btrim(source_reference)) > 0)",
            name="ck_tax_rules_approved_fields",
        ),
    )
    op.create_index(op.f("ix_tax_rules_classification_id"), "tax_rules", ["classification_id"])
    op.execute(
        "ALTER TABLE tax_rules ADD CONSTRAINT ex_tax_rules_no_approved_overlap "
        "EXCLUDE USING gist (classification_id WITH =, "
        "daterange(effective_from, effective_to, '[)') WITH &&) WHERE (status = 'approved')"
    )

    op.create_table(
        "gst_registrations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("gstin", sa.String(length=15), nullable=False),
        sa.Column("legal_name", sa.String(length=200), nullable=False),
        sa.Column("state_code", sa.String(length=2), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="draft", nullable=False),
        *_approval_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("gstin"),
        sa.CheckConstraint(STATUS_OK, name="ck_gst_registrations_status"),
        sa.CheckConstraint(
            "gstin ~ '^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$'",
            name="ck_gst_registrations_gstin_format",
        ),
        sa.CheckConstraint("state_code ~ '^[0-9]{2}$'", name="ck_gst_registrations_state_code"),
        sa.CheckConstraint("substr(gstin, 1, 2) = state_code", name="ck_gst_registrations_gstin_state"),
        sa.CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="ck_gst_registrations_dates"),
        sa.CheckConstraint(
            "status <> 'approved' OR approved_at IS NOT NULL", name="ck_gst_registrations_approved_fields"
        ),
    )


def downgrade() -> None:
    op.drop_table("gst_registrations")
    op.drop_table("tax_rules")
    op.drop_index(op.f("ix_tax_classifications_code"), table_name="tax_classifications")
    op.drop_table("tax_classifications")
    # btree_gist is left installed: other objects may rely on it.
