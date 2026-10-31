import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator

from app.modules.tax.gst_states import BY_CODE


class BusinessProfileWrite(BaseModel):
    """The seller details printed on invoices. The legal name and GSTIN come from the approved GST registration, not from here."""

    model_config = {"extra": "forbid"}

    trade_name: str = Field(min_length=1, max_length=200)
    address_line1: str = Field(min_length=1, max_length=255)
    address_line2: str | None = Field(default=None, max_length=255)
    city: str = Field(min_length=1, max_length=100)
    state_code: str = Field(pattern=r"^[0-9]{2}$", description="Two-digit GST state code (see GET /store/states)")
    pincode: str = Field(pattern=r"^[0-9]{6}$")
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=30)
    invoice_prefix: str = Field(default="INV", pattern=r"^[A-Z0-9]{1,5}$", description="Up to 5 capital letters or digits; invoice numbers are at most 16 characters")
    footer: str | None = Field(default=None, max_length=500)

    @field_validator("state_code")
    @classmethod
    def _known_state(cls, v: str) -> str:
        if v not in BY_CODE:
            raise ValueError("not a GST state code")
        return v


class BusinessProfileRead(BusinessProfileWrite):
    updated_at: datetime
    model_config = {"from_attributes": True}


class InvoiceSummary(BaseModel):
    id: uuid.UUID
    order_id: uuid.UUID
    doc_type: str                 # invoice | credit_note
    number: str
    issued_on: date
    total_paise: int
    currency: str
    related_invoice_id: uuid.UUID | None = None

    model_config = {"from_attributes": True}


class InvoiceRead(InvoiceSummary):
    fiscal_year: str
    document: dict = Field(description="Everything printed on the document, frozen when it was issued")
