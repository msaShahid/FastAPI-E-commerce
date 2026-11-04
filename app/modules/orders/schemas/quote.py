"""POST /cart/quote: what checkout WOULD charge for the cart as it is now. Nothing is reserved, written or used up."""

from typing import Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

from app.modules.orders.schemas.order import ShippingAddressPayload


class QuoteRequest(BaseModel):
    """Everything is optional: with no address the quote still answers, and says which amounts need one."""

    coupon_code: str | None = Field(default=None, max_length=40)
    address_id: int | None = None                      # a saved address (signed-in only)
    shipping_address: ShippingAddressPayload | None = None
    guest_email: EmailStr | None = None                # only matters to a coupon that limits uses per email

    @field_validator("coupon_code")
    @classmethod
    def _clean_coupon_code(cls, v: str | None) -> str | None:
        v = (v or "").strip().upper()
        return v or None

    @model_validator(mode="after")
    def _one_address_source(self) -> "QuoteRequest":
        if self.address_id is not None and self.shipping_address is not None:
            raise ValueError("Provide either address_id or shipping_address, not both")
        return self


class QuoteLine(BaseModel):
    item_id: int
    line_total_cents: int        # quantity x the CURRENT price, which is what checkout charges (it can differ from the cart's own line total)
    discount_cents: int = 0      # this line's share of the coupon discount


class QuoteTax(BaseModel):
    """`status` is "live" when checkout would charge tax now (GST or the interim flat rate), "not_active" when tax is not configured.
    `added_cents` is None while it cannot be known yet; `reason` then says why (address_required | not_configured | cart_empty).
    Tax is ADDED to the total today; `included_cents` is for tax-inclusive pricing and is 0 until that is switched on."""

    status: Literal["live", "not_active"]
    engine: str                  # gst | flat_interim | not_configured
    added_cents: int | None = None
    included_cents: int = 0
    reason: str | None = None


class QuoteBlocking(BaseModel):
    item_id: int
    reason: str                  # unavailable | out_of_stock | insufficient_stock: checkout would refuse this line


class CartQuote(BaseModel):
    currency: str
    minor_unit_exponent: int
    lines: list[QuoteLine]
    subtotal_cents: int
    discount_cents: int
    coupon_code: str | None = None
    shipping_cents: int | None = None      # None when shipping is not configured
    shipping_reason: str | None = None
    tax: QuoteTax
    total_cents: int | None = None         # None while shipping or tax cannot be known
    blocking: list[QuoteBlocking] = []
    price_changed: bool = False            # a line's price moved since it was added: checkout asks the customer to confirm first
    requires_address: bool = False         # true when an address would change the answer (GST depends on the delivery state)
