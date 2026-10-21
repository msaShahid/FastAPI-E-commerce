from pydantic import BaseModel, Field


class StoreCurrencyUpdate(BaseModel):
    currency: str = Field(min_length=3, max_length=3, description="ISO 4217 code, e.g. inr")


class StoreCurrencyRead(BaseModel):
    """null/null until the administrator sets it (checkout answers 503 until then)."""

    currency: str | None
    minor_unit_exponent: int | None
    payment_verified: bool | None
    # False = this is the default for a store that has not chosen one (it becomes fixed at the first order).
    configured: bool = False
    # Why payments in this currency would be refused (checked against the platform's verified providers and the
    # deployment's PAYMENT_CURRENCIES_ENABLED). Empty = no known obstacle.
    warnings: list[str] = []
