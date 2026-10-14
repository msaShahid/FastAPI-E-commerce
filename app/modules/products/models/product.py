from typing import TYPE_CHECKING

from sqlalchemy import DDL, CheckConstraint, Enum, ForeignKey, Index, Integer, String, Text, event
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.shared.enums.product_status import ProductStatus
from app.shared.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.modules.categories.models.category import Category

class Product(Base, TimestampMixin):

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(String(200), index=True)
    slug: Mapped[str] = mapped_column(String(220), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    image_path: Mapped[str | None] = mapped_column(String(255), default=None)
    price_cents: Mapped[int] = mapped_column(Integer, index=True)

    sku: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    stock: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    status: Mapped[ProductStatus] = mapped_column(
        Enum(
            ProductStatus,
            name="product_status",
            values_callable=lambda enum_cls: [item.value for item in enum_cls],
        ),
        default=ProductStatus.DRAFT,
        server_default=ProductStatus.DRAFT.value,
    )

    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"),
    )
    # category is the parent of the product
    category: Mapped["Category"] = relationship(back_populates="products")

    __table_args__ = (
        CheckConstraint("price_cents > 0", name="ck_products_price_positive"),
        CheckConstraint("stock >= 0", name="ck_products_stock_non_negative"),
        # Default storefront listing: WHERE status = 'active' ORDER BY created_at DESC.
        Index("ix_products_status_created_at", "status", "created_at"),
        # Admin listing (all statuses), newest first: no status filter, so the
        # (status, created_at) index above cannot serve it.
        Index("ix_products_created_at", "created_at"),
        # Same listing filtered by category.
        Index(
            "ix_products_category_status_created",
            "category_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_products_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
    )

event.listen(
    Base.metadata,
    "before_create",
    DDL("CREATE EXTENSION IF NOT EXISTS pg_trgm"),
)
