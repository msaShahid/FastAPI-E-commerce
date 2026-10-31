import re
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

_CODE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,31}$")
Kind = Literal["percentage", "fixed", "free_shipping"]


def validate_value_fields(kind: str, percent_off, amount_off_cents, max_discount_cents) -> None:
    """Each kind carries exactly the fields it needs (the database enforces the same)."""
    if kind == "percentage":
        if percent_off is None or amount_off_cents is not None:
            raise ValueError("a percentage coupon needs percent_off (and no amount_off_cents)")
        if not (Decimal("0.01") <= Decimal(percent_off) <= 100):
            raise ValueError("percent_off must be between 0.01 and 100")
    elif kind == "fixed":
        if amount_off_cents is None or amount_off_cents <= 0 or percent_off is not None or max_discount_cents is not None:
            raise ValueError("a fixed coupon needs a positive amount_off_cents (and no percent_off or max_discount_cents)")
    elif any(v is not None for v in (percent_off, amount_off_cents, max_discount_cents)):
        raise ValueError("a free-shipping coupon has no percent_off, amount_off_cents or max_discount_cents")


class CouponBase(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=100, description="An internal label (customers never see it)")
    description: str | None = Field(default=None, max_length=1000)
    kind: Kind
    percent_off: Decimal | None = Field(default=None, max_digits=5, decimal_places=2)
    amount_off_cents: int | None = Field(default=None, gt=0)
    max_discount_cents: int | None = Field(default=None, gt=0, description="Cap on a percentage coupon's discount")
    min_subtotal_cents: int = Field(default=0, ge=0, description="The goods must cost at least this much")
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    max_uses_total: int | None = Field(default=None, gt=0)
    max_uses_per_customer: int | None = Field(default=None, gt=0)
    active: bool = True


class CouponCreate(CouponBase):
    code: str = Field(description="3 to 32 characters: letters, digits, _ and -. Customers may type it in any case.")

    @field_validator("code")
    @classmethod
    def _code(cls, v: str) -> str:
        v = v.strip().upper()
        if not _CODE.fullmatch(v):
            raise ValueError("a code is 3-32 characters: letters, digits, _ or - (starting with a letter or digit)")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> "CouponCreate":
        validate_value_fields(self.kind, self.percent_off, self.amount_off_cents, self.max_discount_cents)
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class CouponUpdate(BaseModel):
    """Any of these. What the coupon GIVES (kind, percent, amount, cap) can only change while no order has used it."""

    model_config = {"extra": "forbid"}

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    kind: Kind | None = None
    percent_off: Decimal | None = Field(default=None, max_digits=5, decimal_places=2)
    amount_off_cents: int | None = Field(default=None, gt=0)
    max_discount_cents: int | None = Field(default=None, gt=0)
    min_subtotal_cents: int | None = Field(default=None, ge=0)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    max_uses_total: int | None = Field(default=None, gt=0)
    max_uses_per_customer: int | None = Field(default=None, gt=0)
    active: bool | None = None


class CouponRead(CouponBase):
    id: int
    code: str
    uses: int = Field(description="Orders using it that are still alive (pending, paid or shipped)")
    state: str = Field(description="active | disabled | scheduled | expired | exhausted")
    created_at: datetime


class CouponCheck(BaseModel):
    model_config = {"extra": "forbid"}

    code: str = Field(min_length=1, max_length=40)
    guest_email: str | None = Field(default=None, max_length=255, description="A guest's email, so a per-customer limit can be checked")


class CouponPreview(BaseModel):
    """What the coupon would do to the cart as it is now. Tax is worked out at checkout, so there is no tax figure here."""

    code: str
    kind: str
    subtotal_cents: int
    discount_cents: int
    subtotal_after_discount_cents: int
    shipping_cents: int | None          # the shipping charge after the coupon (null when shipping is not configured)
    shipping_discount_cents: int


class RedemptionRead(BaseModel):
    order_id: uuid.UUID
    status: str
    discount_cents: int
    total_cents: int
    user_id: uuid.UUID | None
    guest_email: str | None
    created_at: datetime
