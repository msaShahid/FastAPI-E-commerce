from datetime import datetime

from fastapi import Query
from pydantic import BaseModel, Field, field_validator

from app.shared.enums.product_status import ProductStatus
from app.shared.enums.tax import ProductTaxState
from app.shared.pagination.schemas import PageParams


class CategorySummary(BaseModel):
    id: int
    name: str
    slug: str

    model_config = {"from_attributes": True}


class TaxClassificationSummary(BaseModel):
    id: int
    kind: str
    code: str
    description: str
    status: str

    model_config = {"from_attributes": True}


def _normalize_unit(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().upper()
    if not (2 <= len(value) <= 8) or not value.isascii() or not value.isalpha():
        raise ValueError("unit must be 2-8 letters (a unit quantity code such as the one printed on invoices)")
    return value


class ProductCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    price_cents: int = Field(gt=0, description="Price in cents, e.g. 1999 for $19.99")
    sku: str = Field(min_length=1, max_length=64)
    stock: int = Field(default=0, ge=0)
    category_id: int
    status: ProductStatus = ProductStatus.DRAFT
    # Optional: an existing APPROVED classification from the GST master, and the unit.
    tax_classification_id: int | None = Field(default=None, gt=0)
    unit: str | None = Field(default=None, max_length=8)

    _unit = field_validator("unit")(_normalize_unit)


class ProductUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    price_cents: int | None = Field(default=None, gt=0)
    stock: int | None = Field(default=None, ge=0)
    # Required whenever `stock` is sent: every administrative stock change is recorded with who made it and why.
    stock_reason: str | None = Field(default=None, max_length=200)
    category_id: int | None = None
    status: ProductStatus | None = None
    # Send null explicitly to un-assign; omit the field to leave it unchanged.
    tax_classification_id: int | None = Field(default=None, gt=0)
    unit: str | None = Field(default=None, max_length=8)

    _unit = field_validator("unit")(_normalize_unit)


class ProductQueryParams(PageParams):

    category_id: int | None = Query(default=None)
    search: str | None = Query(default=None, max_length=100)
    min_price: int | None = Query(default=None, ge=0, description="Minimum price in cents")
    max_price: int | None = Query(default=None, ge=0, description="Maximum price in cents")
    sort: str = Query(
        default="-created_at",
        pattern="^-?(created_at|price_cents|name)$",
        description="One of: created_at, -created_at, price_cents, -price_cents, name, -name",
    )


class VariantOptionRead(BaseModel):
    name: str
    value: str


class OptionRead(BaseModel):
    id: int = 0
    name: str
    values: list[str]
    value_ids: list[int] = []         # the ids of `values`, in the same order (administrators need them to rename or remove a value)


class VariantRead(BaseModel):
    """One sellable combination. `price_cents` is the EFFECTIVE price the backend will charge: the shopper submits only the variant id."""

    id: int
    sku: str
    options: list[VariantOptionRead]
    price_cents: int
    price_overridden: bool
    available: bool
    availability: str                 # in_stock | low_stock | out_of_stock
    stock: int | None = None          # exact stock: administrators only (or everyone if PUBLIC_STOCK_VISIBILITY=exact)
    status: str | None = None         # administrators only (the public sees active variants only)


class ProductRead(BaseModel):
    id: int
    name: str
    slug: str
    description: str | None
    # For a simple product: its price. For a product with options: the LOWEST effective price ("from"); see price_to_cents.
    price_cents: int
    sku: str
    # Exact stock: administrators only by default (null for the public). The public gets `available` / `availability` instead.
    stock: int | None = None
    available: bool = True
    availability: str = "in_stock"    # in_stock | low_stock | out_of_stock
    has_variants: bool = False
    variant_count: int = 0
    price_from_cents: int | None = None
    price_to_cents: int | None = None
    has_price_range: bool = False
    base_price_cents: int | None = None   # administrators only: the product's own price (variants may override it)
    options: list[OptionRead] | None = None      # product detail only
    variants: list[VariantRead] | None = None    # product detail only
    status: ProductStatus
    category: CategorySummary
    image_path: str | None
    image_url: str | None = None
    # Administrator-only (null for everyone else, including the cached public views).
    unit: str | None = None
    tax_classification: TaxClassificationSummary | None = None
    tax_state: ProductTaxState | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}