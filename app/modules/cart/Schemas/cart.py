from pydantic import BaseModel, Field, model_validator

from app.modules.products.schemas.product import VariantOptionRead


class CartItemCreate(BaseModel):
    """Add a product, or one variant of a product that has options. For a product with options send `variant_id` (`product_id` may be
    sent too and must then match). The price is never sent: the backend uses the current effective price."""

    product_id: int | None = None
    variant_id: int | None = None
    quantity: int = Field(default=1, gt=0)

    @model_validator(mode="after")
    def _one_of_product_or_variant(self) -> "CartItemCreate":
        if self.product_id is None and self.variant_id is None:
            raise ValueError("send product_id (a simple product) or variant_id (a product with options)")
        return self


class CartItemUpdate(BaseModel):
    quantity: int = Field(gt=0)


class CartItemProductSummary(BaseModel):
    id: int
    name: str
    slug: str
    price_cents: int 

    model_config = {"from_attributes": True}


class CartItemVariantSummary(BaseModel):
    id: int
    sku: str
    options: list[VariantOptionRead]


class CartItemRead(BaseModel):
    id: int
    product: CartItemProductSummary          # price_cents here is the PRODUCT's own price; see current_price_cents for this line
    variant: CartItemVariantSummary | None = None
    quantity: int
    price_cents_snapshot: int
    current_price_cents: int | None = None   # what this line would cost per unit right now (the variant's effective price)
    price_changed: bool
    line_total_cents: int
    available: bool = True                   # can this line be bought right now, in this quantity?
    unavailable_reason: str | None = None    # unavailable | out_of_stock | insufficient_stock


class CartRead(BaseModel):
    items: list[CartItemRead]
    subtotal_cents: int
    item_count: int
    cart_digest: str | None = None           # fingerprint of the lines (product, variant, quantity), not of prices