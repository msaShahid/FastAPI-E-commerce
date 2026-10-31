import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Reason = Literal["damaged", "defective", "wrong_item", "not_as_described", "size_or_fit", "changed_mind", "other"]
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _text(value: str | None) -> str | None:
    if value is None:
        return None
    value = _CONTROL.sub("", value).strip()
    return value or None


class ReturnItemCreate(BaseModel):
    model_config = {"extra": "forbid"}

    order_item_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=1000)
    reason: Reason


class ReturnCreate(BaseModel):
    """What the customer wants to send back. Plain text only (an administrator's screen must escape it)."""

    model_config = {"extra": "forbid"}

    items: list[ReturnItemCreate] = Field(min_length=1, max_length=50)
    comment: str | None = Field(default=None, max_length=1000)

    _comment = field_validator("comment")(lambda cls, v: _text(v))

    @model_validator(mode="after")
    def _no_duplicate_lines(self) -> "ReturnCreate":
        if len({i.order_item_id for i in self.items}) != len(self.items):
            raise ValueError("each order item can appear once: send one quantity per item")
        return self


class VariantOption(BaseModel):
    name: str
    value: str


class ReturnItemRead(BaseModel):
    id: int
    order_item_id: int
    product_name: str
    sku: str | None
    options: list[VariantOption] = []
    quantity: int
    reason: str
    received_quantity: int | None = None
    restock: bool = False
    restocked_quantity: int = 0
    condition_note: str | None = None


class RefundSummary(BaseModel):
    id: uuid.UUID
    status: str


class ReturnRead(BaseModel):
    id: uuid.UUID
    order_id: uuid.UUID
    status: str                      # requested | approved | rejected | received | closed | cancelled
    customer_comment: str | None
    rejection_reason: str | None
    resolution: str | None           # refunded | refunded_outside | replacement_sent | no_refund
    refund: RefundSummary | None = None
    requested_at: datetime
    decided_at: datetime | None
    received_at: datetime | None
    closed_at: datetime | None
    items: list[ReturnItemRead]
    # administrators only:
    admin_note: str | None = None
    resolution_note: str | None = None
    user_id: uuid.UUID | None = None


class EligibilityItem(BaseModel):
    order_item_id: int
    product_name: str
    sku: str | None
    options: list[VariantOption] = []
    purchased: int
    returnable: int


class EligibilityRead(BaseModel):
    eligible: bool
    reason_code: str | None = None   # returns_disabled | order_not_shipped | return_window_closed | nothing_returnable
    reason: str | None = None
    window_days: int
    window_ends_at: datetime | None = None
    items: list[EligibilityItem] = []


class ReturnApprove(BaseModel):
    model_config = {"extra": "forbid"}

    note: str | None = Field(default=None, max_length=1000)

    _note = field_validator("note")(lambda cls, v: _text(v))


class ReturnReject(BaseModel):
    model_config = {"extra": "forbid"}

    reason: str = Field(min_length=3, max_length=300, description="Shown to the customer")

    _reason = field_validator("reason")(lambda cls, v: _text(v) or "")


class ReceiveItem(BaseModel):
    model_config = {"extra": "forbid"}

    order_item_id: int = Field(gt=0)
    received_quantity: int = Field(ge=0, description="What actually arrived (at most what was requested)")
    restock: bool = Field(description="Put the received units back in stock (only for goods in sellable condition)")
    condition_note: str | None = Field(default=None, max_length=500)

    _note = field_validator("condition_note")(lambda cls, v: _text(v))


class ReturnReceive(BaseModel):
    """What arrived, for EVERY line of the return."""

    model_config = {"extra": "forbid"}

    items: list[ReceiveItem] = Field(min_length=1, max_length=50)


class ReturnClose(BaseModel):
    model_config = {"extra": "forbid"}

    resolution: Literal["refunded_outside", "replacement_sent", "no_refund"]
    note: str = Field(min_length=3, max_length=1000, description="What was done (for refunded_outside: how much, and where)")

    _note = field_validator("note")(lambda cls, v: _text(v) or "")
