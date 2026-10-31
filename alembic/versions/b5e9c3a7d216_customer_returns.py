"""customer returns

Revision ID: b5e9c3a7d216
Revises: a8d2f6c1e045
Create Date: 2026-11-01 10:00:00.000000

Additive: two new tables (return requests and their items). No existing table or row is touched.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b5e9c3a7d216"
down_revision: Union[str, Sequence[str], None] = "a8d2f6c1e045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STATUSES = ("requested", "approved", "rejected", "received", "closed", "cancelled")
REASONS = ("damaged", "defective", "wrong_item", "not_as_described", "size_or_fit", "changed_mind", "other")
RESOLUTIONS = ("refunded", "refunded_outside", "replacement_sent", "no_refund")


def _in(values) -> str:
    return ", ".join(repr(v) for v in values)


def upgrade() -> None:
    op.create_table(
        "return_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("order_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="requested", nullable=False),
        sa.Column("customer_comment", sa.Text(), nullable=True),
        sa.Column("admin_note", sa.Text(), nullable=True),
        sa.Column("rejection_reason", sa.String(length=300), nullable=True),
        sa.Column("resolution", sa.String(length=20), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("refund_id", sa.Uuid(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handled_by_user_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(f"status IN ({_in(STATUSES)})", name="ck_return_requests_status"),
        sa.CheckConstraint(f"resolution IS NULL OR resolution IN ({_in(RESOLUTIONS)})", name="ck_return_requests_resolution"),
        sa.CheckConstraint("status <> 'rejected' OR (rejection_reason IS NOT NULL AND length(btrim(rejection_reason)) >= 3)", name="ck_return_requests_rejection"),
        sa.CheckConstraint("status <> 'closed' OR resolution IS NOT NULL", name="ck_return_requests_closed_resolution"),
        sa.CheckConstraint("customer_comment IS NULL OR length(customer_comment) <= 1000", name="ck_return_requests_comment"),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["refund_id"], ["refunds.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["handled_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_return_requests_order", "return_requests", ["order_id"])
    op.create_index("ix_return_requests_status_requested", "return_requests", ["status", "requested_at"])
    op.create_index("ix_return_requests_user_requested", "return_requests", ["user_id", "requested_at"])
    op.create_table(
        "return_request_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("return_id", sa.Uuid(), nullable=False),
        sa.Column("order_item_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=20), nullable=False),
        sa.Column("received_quantity", sa.Integer(), nullable=True),
        sa.Column("restock", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("restocked_quantity", sa.Integer(), server_default="0", nullable=False),
        sa.Column("condition_note", sa.Text(), nullable=True),
        sa.CheckConstraint("quantity > 0", name="ck_return_request_items_quantity"),
        sa.CheckConstraint(f"reason IN ({_in(REASONS)})", name="ck_return_request_items_reason"),
        sa.CheckConstraint("received_quantity IS NULL OR (received_quantity >= 0 AND received_quantity <= quantity)", name="ck_return_request_items_received"),
        sa.CheckConstraint("restocked_quantity >= 0 AND restocked_quantity <= coalesce(received_quantity, 0)", name="ck_return_request_items_restocked"),
        sa.ForeignKeyConstraint(["return_id"], ["return_requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("return_id", "order_item_id", name="uq_return_request_items_line"),
    )
    op.create_index("ix_return_request_items_order_item", "return_request_items", ["order_item_id"])


def downgrade() -> None:
    op.drop_index("ix_return_request_items_order_item", table_name="return_request_items")
    op.drop_table("return_request_items")
    op.drop_index("ix_return_requests_user_requested", table_name="return_requests")
    op.drop_index("ix_return_requests_status_requested", table_name="return_requests")
    op.drop_index("ix_return_requests_order", table_name="return_requests")
    op.drop_table("return_requests")
