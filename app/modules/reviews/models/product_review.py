import uuid

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.shared.mixins import TimestampMixin


class ProductReview(Base, TimestampMixin):
    """A customer's review of a product they BOUGHT: one per customer per product, a 1-5 rating and optional plain text.
    Visible to everyone only while `status` is 'published'; an administrator can hide one (with a reason) or delete it."""

    __tablename__ = "product_reviews"
    __table_args__ = (
        UniqueConstraint("product_id", "user_id", name="uq_product_reviews_product_user"),
        CheckConstraint("rating BETWEEN 1 AND 5", name="ck_product_reviews_rating"),
        CheckConstraint("status IN ('published', 'hidden')", name="ck_product_reviews_status"),
        CheckConstraint("title IS NULL OR (length(btrim(title)) > 0 AND length(title) <= 100)", name="ck_product_reviews_title"),
        CheckConstraint("body IS NULL OR (length(btrim(body)) > 0 AND length(body) <= 2000)", name="ck_product_reviews_body"),
        CheckConstraint("status <> 'hidden' OR length(btrim(hidden_reason)) >= 3", name="ck_product_reviews_hidden_reason"),
        Index("ix_product_reviews_product_status_created", "product_id", "status", "created_at"),
        Index("ix_product_reviews_status_created", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    rating: Mapped[int] = mapped_column(SmallInteger)
    title: Mapped[str | None] = mapped_column(String(100), default=None)
    body: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(String(10), default="published", server_default="published")
    hidden_reason: Mapped[str | None] = mapped_column(String(200), default=None)
    hidden_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), default=None)
