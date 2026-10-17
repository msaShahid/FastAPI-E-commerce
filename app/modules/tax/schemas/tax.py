import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, Field, model_validator

from app.shared.enums.tax import ClassificationKind, ProductTaxState, TaxRecordStatus, TaxTreatment

Rate = Annotated[Decimal, Field(ge=0, le=100, max_digits=6, decimal_places=3)]
_GSTIN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


class _Audited(BaseModel):
    status: TaxRecordStatus
    created_by_user_id: uuid.UUID | None
    approved_by_user_id: uuid.UUID | None
    approved_at: datetime | None
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


class ApproveRequest(BaseModel):
    """`note` is mandatory only when self-approval is explicitly enabled and used."""

    note: str | None = Field(default=None, max_length=1000)


# ---------------------------------------------------------------- classifications
class ClassificationCreate(BaseModel):
    kind: ClassificationKind
    code: str = Field(pattern=r"^[0-9]+$", max_length=8)
    description: str = Field(min_length=1, max_length=500)
    source_reference: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _code_length(self) -> "ClassificationCreate":
        # Format only. Whether a code is a real, applicable classification is for the GST professional.
        if self.kind == ClassificationKind.HSN and len(self.code) not in (2, 4, 6, 8):
            raise ValueError("HSN code must have 2, 4, 6 or 8 digits")
        if self.kind == ClassificationKind.SAC and len(self.code) != 6:
            raise ValueError("SAC code must have 6 digits")
        return self


class ClassificationUpdate(BaseModel):
    description: str | None = Field(default=None, min_length=1, max_length=500)
    source_reference: str | None = Field(default=None, max_length=2000)


class ClassificationRead(_Audited):
    id: int
    kind: ClassificationKind
    code: str
    description: str
    source_reference: str | None


class ProductTaxRead(BaseModel):
    """A product and whether it points at an approved classification (administrators only)."""

    id: int
    sku: str
    name: str
    status: str
    unit: str | None
    tax_classification_id: int | None
    tax_state: ProductTaxState


# ---------------------------------------------------------------- rules
def check_rule_shape(treatment: str, cgst: Decimal, sgst: Decimal, igst: Decimal) -> None:
    if treatment == TaxTreatment.TAXABLE:
        if igst <= 0:
            raise ValueError("A taxable rule needs an IGST rate greater than 0")
        if cgst + sgst != igst:
            raise ValueError("CGST + SGST/UTGST must equal IGST")
    elif cgst != 0 or sgst != 0 or igst != 0:
        raise ValueError(f"A '{treatment}' rule must have all rates at 0")


class RuleCreate(BaseModel):
    classification_id: int
    treatment: TaxTreatment
    cgst_rate: Rate = Decimal("0")
    sgst_utgst_rate: Rate = Decimal("0")
    igst_rate: Rate = Decimal("0")
    effective_from: date
    effective_to: date | None = None
    source_reference: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _valid(self) -> "RuleCreate":
        check_rule_shape(self.treatment, self.cgst_rate, self.sgst_utgst_rate, self.igst_rate)
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be after effective_from")
        return self


class RuleUpdate(BaseModel):
    treatment: TaxTreatment | None = None
    cgst_rate: Rate | None = None
    sgst_utgst_rate: Rate | None = None
    igst_rate: Rate | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    source_reference: str | None = Field(default=None, max_length=2000)


class RuleClose(BaseModel):
    effective_to: date


class RuleRead(_Audited):
    id: int
    classification_id: int
    treatment: TaxTreatment
    cgst_rate: Decimal
    sgst_utgst_rate: Decimal
    igst_rate: Decimal
    effective_from: date
    effective_to: date | None
    source_reference: str | None


# ---------------------------------------------------------------- registrations
class RegistrationCreate(BaseModel):
    gstin: str = Field(min_length=15, max_length=15)
    legal_name: str = Field(min_length=1, max_length=200)
    state_code: str = Field(pattern=r"^[0-9]{2}$")
    valid_from: date
    valid_to: date | None = None

    @model_validator(mode="after")
    def _valid(self) -> "RegistrationCreate":
        self.gstin = self.gstin.strip().upper()
        if not _GSTIN.match(self.gstin):
            raise ValueError("GSTIN format is invalid")
        if self.gstin[:2] != self.state_code:
            raise ValueError("The first two digits of the GSTIN must equal state_code")
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        return self


class RegistrationUpdate(BaseModel):
    legal_name: str | None = Field(default=None, min_length=1, max_length=200)
    valid_from: date | None = None
    valid_to: date | None = None


class RegistrationRead(_Audited):
    id: int
    gstin: str
    legal_name: str
    state_code: str
    valid_from: date
    valid_to: date | None
