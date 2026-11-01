from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class DemoPayRequest(BaseModel):
    """Pay with FAKE test details. Send only the one detail that matches the method: the demo gateway never receives an expiry date, a CVC, a
    name or anything else, and refuses any number or id that is not on its published test list (so real details are never accepted)."""

    model_config = {"extra": "forbid"}

    order_id: UUID
    method: Literal["card", "upi", "netbanking"] = "card"
    card_number: str | None = Field(default=None, max_length=40)
    upi_id: str | None = Field(default=None, max_length=60)
    bank: str | None = Field(default=None, max_length=60)

    @model_validator(mode="after")
    def _exactly_the_detail_of_the_method(self) -> "DemoPayRequest":
        wanted = {"card": self.card_number, "upi": self.upi_id, "netbanking": self.bank}
        if not wanted[self.method]:
            raise ValueError(f"send the {'card_number' if self.method == 'card' else 'upi_id' if self.method == 'upi' else 'bank'} for the {self.method} method")
        if sum(v is not None for v in wanted.values()) != 1:
            raise ValueError("send only the detail that belongs to the chosen method")
        return self


class DemoPayResult(BaseModel):
    demo: bool = True
    order_id: UUID
    payment_id: UUID
    succeeded: bool
    payment_status: str
    order_status: str
    failure_code: str | None = None
    message: str


class TestDetail(BaseModel):
    value: str
    outcome: str                       # succeeds | declined
    description: str


class TestDetails(BaseModel):
    demo_active: bool                  # whether the demo gateway is the store's current payment gateway
    notice: str
    cards: list[TestDetail]
    upi_ids: list[TestDetail]
    banks: list[TestDetail]
    other: str                         # what to enter for expiry, CVC and one-time passwords on the storefront's own form
