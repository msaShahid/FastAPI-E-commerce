import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.modules.products.schemas.product import ProductRead

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SKU = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$")


def _clean(value: str, what: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    if not value or _CONTROL.search(value):
        raise ValueError(f"{what} must not be empty or contain control characters")
    return value


class OptionSpec(BaseModel):
    """One option set of a product (for example Size with S, M, L). The first is slot 1, the second slot 2, and so on."""

    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=50)
    values: list[str] = Field(min_length=1, max_length=50)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return _clean(v, "an option name")

    @field_validator("values")
    @classmethod
    def _values(cls, values: list[str]) -> list[str]:
        cleaned = [_clean(v, "an option value") for v in values]
        if any(len(v) > 50 for v in cleaned):
            raise ValueError("an option value is at most 50 characters")
        if len({v.lower() for v in cleaned}) != len(cleaned):
            raise ValueError("an option's values must be different (ignoring letter case)")
        return cleaned


def _unique_option_names(options: list[OptionSpec]) -> list[OptionSpec]:
    if len({o.name.lower() for o in options}) != len(options):
        raise ValueError("option names must be different (ignoring letter case)")
    return options


class OptionsSet(BaseModel):
    """The complete option structure of a product that has no variants yet. An empty list makes it a simple product again."""

    model_config = {"extra": "forbid"}

    options: list[OptionSpec] = Field(max_length=3)

    _unique = field_validator("options")(_unique_option_names)


class VariantSpec(BaseModel):
    """One sellable combination: a value for EVERY option, its own SKU and stock, and optionally its own price. Nothing is generated for you."""

    model_config = {"extra": "forbid"}

    sku: str
    option_values: dict[str, str] = Field(min_length=1, max_length=3)    # {"Size": "M", "Colour": "Blue"}
    price_override_cents: int | None = Field(default=None, gt=0)          # null: the product's price
    stock: int = Field(default=0, ge=0)
    status: Literal["active", "inactive"] = "active"

    @field_validator("sku")
    @classmethod
    def _sku(cls, v: str) -> str:
        v = v.strip()
        if not _SKU.fullmatch(v):
            raise ValueError("a variant SKU is 1-64 characters: letters, digits and . _ / - (starting with a letter or digit)")
        return v

    @field_validator("option_values")
    @classmethod
    def _option_values(cls, v: dict[str, str]) -> dict[str, str]:
        cleaned = {_clean(k, "an option name"): _clean(val, "an option value") for k, val in v.items()}
        if len({k.lower() for k in cleaned}) != len(cleaned):
            raise ValueError("an option appears twice")
        return cleaned


class VariantsBulk(BaseModel):
    """Up to 100 variants, created together or not at all."""

    model_config = {"extra": "forbid"}

    variants: list[VariantSpec] = Field(min_length=1, max_length=100)


class VariantPatch(BaseModel):
    """Only the price and the status can change. The SKU and the option values never do (create a new variant instead), and stock changes
    go through the stock endpoint so that each one is recorded with a reason."""

    model_config = {"extra": "forbid"}

    price_override_cents: int | None = Field(default=None, gt=0)    # send null to remove the override
    status: Literal["active", "inactive"] | None = None


class ValueAdd(BaseModel):
    model_config = {"extra": "forbid"}

    value: str = Field(min_length=1, max_length=50)

    _v = field_validator("value")(lambda cls, v: _clean(v, "an option value"))


class StockAdjust(BaseModel):
    """Set stock to an exact number, or add/remove a number of units. A reason is mandatory: it is recorded with who and when."""

    model_config = {"extra": "forbid"}

    set_to: int | None = Field(default=None, ge=0)
    delta: int | None = None
    reason: str = Field(min_length=3, max_length=200)

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 3 or _CONTROL.search(v):
            raise ValueError("give a reason of at least 3 characters")
        return v

    @model_validator(mode="after")
    def _one_of(self) -> "StockAdjust":
        if (self.set_to is None) == (self.delta is None):
            raise ValueError("send exactly one of set_to or delta")
        if self.delta == 0:
            raise ValueError("delta must not be zero")
        return self


class StockAdjustmentResult(BaseModel):
    product_id: int
    variant_id: int | None
    before_qty: int
    after_qty: int
    delta: int
    changed: bool           # false when set_to equalled the current stock: nothing happened and nothing was recorded


class StockMovementRead(BaseModel):
    id: int
    occurred_at: datetime
    kind: str
    product_id: int
    variant_id: int | None
    delta: int
    before_qty: int
    after_qty: int
    actor_user_id: uuid.UUID | None
    reason: str | None
    order_id: uuid.UUID | None

    model_config = {"from_attributes": True}


class MissingCombinations(BaseModel):
    total: int
    truncated: bool
    items: list[dict[str, str]]


class ConvertRequest(BaseModel):
    """Turn a simple product into a product with options, moving its stock onto the variants in one all-or-nothing step."""

    model_config = {"extra": "forbid"}

    options: list[OptionSpec] = Field(min_length=1, max_length=3)
    variants: list[VariantSpec] = Field(min_length=1, max_length=100)
    expected_stock: int = Field(ge=0, description="The product's stock as you saw it. If it has changed, nothing is converted.")
    reason: str = Field(min_length=3, max_length=200)

    _unique = field_validator("options")(_unique_option_names)

    @field_validator("reason")
    @classmethod
    def _reason(cls, v: str) -> str:
        return _clean(v, "the reason")


class ConversionResult(BaseModel):
    product: ProductRead
    cart_lines_removed: int
