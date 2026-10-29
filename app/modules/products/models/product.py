from typing import TYPE_CHECKING

from sqlalchemy import (
    DDL,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.shared.enums.product_status import ProductStatus
from app.shared.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.modules.categories.models.category import Category
    from app.modules.tax.models.tax import TaxClassification

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

    # Approved HSN/SAC classification (GST master). NULL = unclassified, which is never
    # treated as tax-ready. A database trigger only allows an APPROVED classification, and
    # only allows retiring a classification that no product uses.
    tax_classification_id: Mapped[int | None] = mapped_column(
        ForeignKey("tax_classifications.id", ondelete="RESTRICT"), index=True, default=None
    )
    # Unit quantity code for the product (format only: 2-8 upper-case letters). Checking it
    # against the official list is a business/professional decision, not made here.
    unit: Mapped[str | None] = mapped_column(String(8), default=None)
    # 0 = a simple product (own SKU, stock and price, exactly as before). 1..3 = sold only through its variants (gap E).
    option_count: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
    tax_classification: Mapped["TaxClassification | None"] = relationship()
    # category is the parent of the product
    category: Mapped["Category"] = relationship(back_populates="products")

    __table_args__ = (
        CheckConstraint("price_cents > 0", name="ck_products_price_positive"),
        CheckConstraint("stock >= 0", name="ck_products_stock_non_negative"),
        CheckConstraint("unit IS NULL OR unit ~ '^[A-Z]{2,8}$'", name="ck_products_unit_format"),
        CheckConstraint("option_count BETWEEN 0 AND 3", name="ck_products_option_count"),
        # A product with variants keeps its stock on the variants: the product's own stock can never disagree with them.
        CheckConstraint("option_count = 0 OR stock = 0", name="ck_products_stock_zero_with_options"),
        UniqueConstraint("id", "option_count", name="uq_products_id_option_count"),
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

# Variant, SKU-registry and stock-ledger tables (gap E) belong to the same schema: always import them together.
from app.modules.products.models import stock_movement as _stock_movement  # noqa: E402,F401
from app.modules.products.models import variants as _variants  # noqa: E402,F401
