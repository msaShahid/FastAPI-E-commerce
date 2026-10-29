"""Product options and variants (gap E). A product with `option_count > 0` is sold only through its variants; a product with
option_count = 0 is a simple product and behaves exactly as before.

Integrity is declarative wherever PostgreSQL allows it: composite foreign keys tie every option, value and variant to ONE product and
every value to its option's slot; one unique index forbids duplicate combinations; CHECKs keep stock and prices valid. The few things a
constraint cannot express are triggers (variant_guard.py)."""

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.shared.mixins import TimestampMixin


class ProductOption(Base, TimestampMixin):
    """One option set of one product (for example Size). `position` 1..3 is its slot in every variant."""

    __tablename__ = "product_options"
    __table_args__ = (
        CheckConstraint("position BETWEEN 1 AND 3", name="ck_product_options_position"),
        CheckConstraint("btrim(name) <> '' AND name = btrim(name)", name="ck_product_options_name"),
        UniqueConstraint("product_id", "position", name="uq_product_options_product_position"),
        UniqueConstraint("id", "product_id", "position", name="uq_product_options_id_product_position"),
        Index("ux_product_options_product_lname", "product_id", text("lower(name)"), unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(SmallInteger)
    name: Mapped[str] = mapped_column(String(50))
    values: Mapped[list["ProductOptionValue"]] = relationship(
        "ProductOptionValue", primaryjoin="ProductOption.id == foreign(ProductOptionValue.option_id)", viewonly=True,
        order_by="ProductOptionValue.id", lazy="raise_on_sql",
    )


class ProductOptionValue(Base, TimestampMixin):
    """One value of an option (for example M). Carries the product and the option's slot so a variant's composite foreign key can
    prove that the value belongs to THIS product's option in THAT slot."""

    __tablename__ = "product_option_values"
    __table_args__ = (
        ForeignKeyConstraint(
            ["option_id", "product_id", "option_position"],
            ["product_options.id", "product_options.product_id", "product_options.position"],
            name="fk_option_values_option", ondelete="CASCADE", onupdate="RESTRICT",
        ),
        CheckConstraint("btrim(value) <> '' AND value = btrim(value)", name="ck_option_values_value"),
        UniqueConstraint("id", "product_id", "option_position", name="uq_option_values_id_product_position"),
        Index("ux_option_values_option_lvalue", "option_id", text("lower(value)"), unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    option_id: Mapped[int] = mapped_column(Integer)
    product_id: Mapped[int] = mapped_column(Integer)
    option_position: Mapped[int] = mapped_column(SmallInteger)
    value: Mapped[str] = mapped_column(String(50))


class ProductVariant(Base, TimestampMixin):
    """One sellable combination: its own SKU, stock and (optionally) price. Never auto-generated: only combinations an administrator
    creates exist."""

    __tablename__ = "product_variants"
    __table_args__ = (
        ForeignKeyConstraint(["product_id"], ["products.id"], name="fk_variants_product", ondelete="RESTRICT"),
        # The product's declared option count cannot change while variants exist (ON UPDATE RESTRICT).
        ForeignKeyConstraint(
            ["product_id", "option_count"], ["products.id", "products.option_count"],
            name="fk_variants_product_option_count", onupdate="RESTRICT", ondelete="RESTRICT",
        ),
        # Slot K may only hold a value of THIS product's K-th option.
        ForeignKeyConstraint(
            ["value1_id", "product_id", "slot1"],
            ["product_option_values.id", "product_option_values.product_id", "product_option_values.option_position"],
            name="fk_variants_value1", onupdate="RESTRICT", ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["value2_id", "product_id", "slot2"],
            ["product_option_values.id", "product_option_values.product_id", "product_option_values.option_position"],
            name="fk_variants_value2", onupdate="RESTRICT", ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["value3_id", "product_id", "slot3"],
            ["product_option_values.id", "product_option_values.product_id", "product_option_values.option_position"],
            name="fk_variants_value3", onupdate="RESTRICT", ondelete="RESTRICT",
        ),
        CheckConstraint("slot1 = 1 AND slot2 = 2 AND slot3 = 3", name="ck_variants_slots"),
        CheckConstraint("option_count BETWEEN 1 AND 3", name="ck_variants_option_count"),
        # A combination is complete: exactly one value per option, nothing in unused slots.
        CheckConstraint(
            "(value1_id IS NOT NULL) = (option_count >= 1) AND (value2_id IS NOT NULL) = (option_count >= 2) "
            "AND (value3_id IS NOT NULL) = (option_count >= 3)",
            name="ck_variants_complete_combination",
        ),
        # No duplicate combinations (NULLs compare equal, so unused slots do not defeat the rule). PostgreSQL 15+.
        UniqueConstraint(
            "product_id", "value1_id", "value2_id", "value3_id", name="uq_variants_combination", postgresql_nulls_not_distinct=True
        ),
        UniqueConstraint("id", "product_id", name="uq_variants_id_product"),
        CheckConstraint("price_override_cents IS NULL OR price_override_cents > 0", name="ck_variants_price_override"),
        CheckConstraint("stock >= 0", name="ck_variants_stock_non_negative"),
        CheckConstraint("status IN ('active', 'inactive')", name="ck_variants_status"),
        CheckConstraint("btrim(sku) <> '' AND sku = btrim(sku)", name="ck_variants_sku"),
        Index("ix_product_variants_product_status", "product_id", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(Integer)
    option_count: Mapped[int] = mapped_column(SmallInteger)
    value1_id: Mapped[int | None] = mapped_column(Integer, default=None)
    value2_id: Mapped[int | None] = mapped_column(Integer, default=None)
    value3_id: Mapped[int | None] = mapped_column(Integer, default=None)
    slot1: Mapped[int] = mapped_column(SmallInteger, default=1, server_default="1")
    slot2: Mapped[int] = mapped_column(SmallInteger, default=2, server_default="2")
    slot3: Mapped[int] = mapped_column(SmallInteger, default=3, server_default="3")
    sku: Mapped[str] = mapped_column(String(64))
    price_override_cents: Mapped[int | None] = mapped_column(Integer, default=None)
    stock: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(10), default="active", server_default="active")


class SkuRegistry(Base):
    """Every product SKU and variant SKU, normalised (upper-case, trimmed). Its primary key makes SKUs unique ACROSS products and variants
    and case-insensitively, including under concurrent writes (the second writer waits, then fails). Maintained by triggers only."""

    __tablename__ = "sku_registry"
    __table_args__ = (CheckConstraint("(product_id IS NULL) <> (variant_id IS NULL)", name="ck_sku_registry_one_owner"),)

    sku_norm: Mapped[str] = mapped_column(String(64), primary_key=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), default=None)
    variant_id: Mapped[int | None] = mapped_column(ForeignKey("product_variants.id", ondelete="CASCADE"), default=None)

# Installs the database guards whenever these models are imported (create_all).
from app.modules.products.models import variant_guard  # noqa: E402,F401
