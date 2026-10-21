"""legacy-currency remediation records and the cancel-with-refund flag

Revision ID: b6f2d9a4c571
Revises: a2e4c8b6d193
Create Date: 2026-10-18 18:00:00.000000

Additive. Adds refunds.cancels_order (default false: every existing refund is an ordinary refund) and the empty
order_currency_remediations table. No existing order, payment or refund is changed, and no currency is assigned to
any legacy order: that only happens through the audited remediation endpoint, with evidence.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b6f2d9a4c571"
down_revision: Union[str, Sequence[str], None] = "a2e4c8b6d193"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("refunds", sa.Column("cancels_order", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.create_table(
        "order_currency_remediations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("evidence_type", sa.String(length=40), nullable=False),
        sa.Column("evidence_reference", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("established_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["established_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_id"),
        sa.CheckConstraint("currency ~ '^[a-z]{3}$'", name="ck_currency_remediations_currency"),
        sa.CheckConstraint(
            "evidence_type IN ('provider_record', 'accounting_record', 'store_history_reviewed')",
            name="ck_currency_remediations_evidence_type",
        ),
        sa.CheckConstraint("length(btrim(evidence_reference)) > 0", name="ck_currency_remediations_reference"),
    )


def downgrade() -> None:
    op.drop_table("order_currency_remediations")
    op.drop_column("refunds", "cancels_order")
