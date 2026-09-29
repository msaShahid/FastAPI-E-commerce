from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, model_validator

from app.shared.enums.order_status import OrderStatus


class OrderItemRead(BaseModel):
    id: int
    product_id: int
    product_name_snapshot: str
    price_cents_snapshot: int
    quantity: int
    line_total_cents: int

    model_config = {"from_attributes": True}


class ShippingAddressRead(BaseModel):
    recipient_name: str | None
    line1: str | None
    line2: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    country: str | None


class ShippingAddressPayload(BaseModel):
    """An inline shipping address supplied directly in a checkout
    request, as opposed to picked by address_id from the caller's
    saved addresses. Required for guest checkout."""

    recipient_name: str = Field(min_length=1, max_length=200)
    line1: str = Field(min_length=1, max_length=255)
    line2: str | None = Field(default=None, max_length=255)
    city: str = Field(min_length=1, max_length=100)
    state: str = Field(min_length=1, max_length=100)
    postal_code: str = Field(min_length=1, max_length=20)
    country: str = Field(min_length=2, max_length=2, description="ISO 3166-1 alpha-2")


class CheckoutRequest(BaseModel):
    # Pick ONE way to ship this order: a saved address (logged-in only)
    # or one supplied here inline (works logged-in or as a guest).
    address_id: int | None = None
    shipping_address: ShippingAddressPayload | None = None

    # Required for a guest checkout (no Authorization header); ignored
    # for a logged-in checkout, which uses the account's own email.
    guest_email: EmailStr | None = None

    @model_validator(mode="after")
    def _one_address_source(self) -> "CheckoutRequest":
        if self.address_id is not None and self.shipping_address is not None:
            raise ValueError("Provide either address_id or shipping_address, not both")
        return self


class OrderRead(BaseModel):
    id: UUID
    user_id: UUID | None
    guest_email: str | None
    status: OrderStatus
    subtotal_cents: int
    shipping_cents: int
    tax_cents: int
    total_cents: int
    shipping_address: ShippingAddressRead
    items: list[OrderItemRead]
    created_at: datetime

    model_config = {"from_attributes": True}

    @model_validator(mode="before")
    @classmethod
    def _build_shipping_address(cls, order):

        if isinstance(order, dict):
            return order
        return {
            "id": order.id,
            "user_id": order.user_id,
            "guest_email": order.guest_email,
            "status": order.status,
            "subtotal_cents": order.subtotal_cents,
            "shipping_cents": order.shipping_cents,
            "tax_cents": order.tax_cents,
            "total_cents": order.total_cents,
            "items": order.items,
            "created_at": order.created_at,
            "shipping_address": {
                "recipient_name": order.shipping_recipient_name,
                "line1": order.shipping_line1,
                "line2": order.shipping_line2,
                "city": order.shipping_city,
                "state": order.shipping_state,
                "postal_code": order.shipping_postal_code,
                "country": order.shipping_country,
            },
        }


class OrderStatusUpdate(BaseModel):
    status: OrderStatus