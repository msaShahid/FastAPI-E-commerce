"""product reviews and wishlists

Revision ID: e3a7b1c9d452
Revises: d2f6b9a4c731
Create Date: 2026-10-31 10:00:00.000000

Additive: two new tables, no existing table or row is touched.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e3a7b1c9d452"
down_revision: Union[str, Sequence[str], None] = "d2f6b9a4c731"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "product_reviews",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("title", sa.String(length=100), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=10), server_default="published", nullable=False),
        sa.Column("hidden_reason", sa.String(length=200), nullable=True),
        sa.Column("hidden_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("rating BETWEEN 1 AND 5", name="ck_product_reviews_rating"),
        sa.CheckConstraint("status IN ('published', 'hidden')", name="ck_product_reviews_status"),
        sa.CheckConstraint("title IS NULL OR (length(btrim(title)) > 0 AND length(title) <= 100)", name="ck_product_reviews_title"),
        sa.CheckConstraint("body IS NULL OR (length(btrim(body)) > 0 AND length(body) <= 2000)", name="ck_product_reviews_body"),
        sa.CheckConstraint("status <> 'hidden' OR length(btrim(hidden_reason)) >= 3", name="ck_product_reviews_hidden_reason"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["hidden_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("product_id", "user_id", name="uq_product_reviews_product_user"),
    )
    op.create_index("ix_product_reviews_product_status_created", "product_reviews", ["product_id", "status", "created_at"])
    op.create_index("ix_product_reviews_status_created", "product_reviews", ["status", "created_at"])
    op.create_table(
        "wishlist_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "product_id", name="uq_wishlist_items_user_product"),
    )
    op.create_index("ix_wishlist_items_user_created", "wishlist_items", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_wishlist_items_user_created", table_name="wishlist_items")
    op.drop_table("wishlist_items")
    op.drop_index("ix_product_reviews_status_created", table_name="product_reviews")
    op.drop_index("ix_product_reviews_product_status_created", table_name="product_reviews")
    op.drop_table("product_reviews")
