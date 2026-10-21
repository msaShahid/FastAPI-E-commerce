"""legacy-currency reviews record a decision and remediation (unresolved outcomes are recorded too)

Revision ID: c8e3a5f7b142
Revises: b6f2d9a4c571
Create Date: 2026-10-18 20:00:00.000000

order_currency_remediations becomes a log of REVIEWS: several per order are allowed, a review has a decision
(established / unresolved_conflicting / unresolved_inconclusive) and the remediation taken, and a currency is
stored only for `established` (at most one per order, by a partial unique index). Existing rows (there are none
before this feature is used) become `established` reviews. No order is changed and no currency is assigned.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c8e3a5f7b142"
down_revision: Union[str, Sequence[str], None] = "b6f2d9a4c571"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("order_currency_remediations", sa.Column("decision", sa.String(length=30), server_default="established", nullable=False))
    op.add_column("order_currency_remediations", sa.Column("remediation", sa.Text(), nullable=True))
    op.alter_column("order_currency_remediations", "currency", existing_type=sa.String(length=3), nullable=True)
    op.drop_constraint("order_currency_remediations_order_id_key", "order_currency_remediations", type_="unique")
    op.create_index(op.f("ix_order_currency_remediations_order_id"), "order_currency_remediations", ["order_id"])
    op.create_index(
        "ux_currency_remediations_one_established", "order_currency_remediations", ["order_id"], unique=True,
        postgresql_where=sa.text("decision = 'established'"),
    )
    op.create_check_constraint(
        "ck_currency_remediations_decision", "order_currency_remediations",
        "decision IN ('established', 'unresolved_conflicting', 'unresolved_inconclusive')",
    )
    op.create_check_constraint(
        "ck_currency_remediations_decision_currency", "order_currency_remediations",
        "(decision = 'established' AND currency IS NOT NULL) OR (decision <> 'established' AND currency IS NULL)",
    )


def downgrade() -> None:
    # Unresolved reviews carry no currency and cannot exist in the older shape: they are removed on downgrade.
    op.execute("DELETE FROM order_currency_remediations WHERE decision <> 'established'")
    op.drop_constraint("ck_currency_remediations_decision_currency", "order_currency_remediations", type_="check")
    op.drop_constraint("ck_currency_remediations_decision", "order_currency_remediations", type_="check")
    op.drop_index("ux_currency_remediations_one_established", table_name="order_currency_remediations")
    op.drop_index(op.f("ix_order_currency_remediations_order_id"), table_name="order_currency_remediations")
    op.create_unique_constraint("order_currency_remediations_order_id_key", "order_currency_remediations", ["order_id"])
    op.alter_column("order_currency_remediations", "currency", existing_type=sa.String(length=3), nullable=False)
    op.drop_column("order_currency_remediations", "remediation")
    op.drop_column("order_currency_remediations", "decision")
