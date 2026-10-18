from pydantic import BaseModel, Field


class StoreCurrencyUpdate(BaseModel):
    currency: str = Field(min_length=3, max_length=3, description="ISO 4217 code, e.g. inr")


class StoreCurrencyRead(BaseModel):
    """null/null until the administrator sets it (checkout answers 503 until then)."""

    currency: str | None
    minor_unit_exponent: int | None
    payment_verified: bool | None
