"""order shipments: carrier, tracking number and URL entered by an administrator

Revision ID: a3e7c1b9d542
Revises: f5a9d3c7e124
Create Date: 2026-10-28 10:00:00.000000

Additive: a new table; no existing data is touched and no existing column changes.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a3e7c1b9d542"
down_revision: Union[str, Sequence[str], None] = "f5a9d3c7e124"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "order_shipments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("carrier_name", sa.String(length=60), nullable=False),
        sa.Column("carrier_code", sa.String(length=30), nullable=True),
        sa.Column("tracking_number", sa.String(length=64), nullable=True),
        sa.Column("tracking_url", sa.String(length=2048), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="shipped", nullable=False),
        sa.Column("shipped_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('shipped', 'delivered')", name="ck_order_shipments_status"),
        sa.CheckConstraint("(status = 'delivered') = (delivered_at IS NOT NULL)", name="ck_order_shipments_delivered_at"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_order_shipments_order_id", "order_shipments", ["order_id"])
    op.create_index(
        "ux_order_shipments_tracking", "order_shipments", ["order_id", "carrier_name", "tracking_number"], unique=True,
        postgresql_where=sa.text("tracking_number IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_order_shipments_tracking", table_name="order_shipments", postgresql_where=sa.text("tracking_number IS NOT NULL"))
    op.drop_index("ix_order_shipments_order_id", table_name="order_shipments")
    op.drop_table("order_shipments")
