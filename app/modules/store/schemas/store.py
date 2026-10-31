from pydantic import BaseModel, Field


class StoreCurrencyUpdate(BaseModel):
    currency: str = Field(min_length=3, max_length=3, description="ISO 4217 code, e.g. inr")


class StoreSettingsRead(BaseModel):
    """Public, read-only facts a storefront needs to describe how buying works. Amounts are in MINOR units of `currency`
    (paise for INR); divide by 10**minor_unit_exponent to display. Never includes tax rates, credentials or provider details."""

    currency: str | None
    minor_unit_exponent: int | None
    flat_shipping_cents: int | None
    free_shipping_threshold_cents: int | None   # null = no free shipping
    pending_order_timeout_minutes: int          # an unpaid order is cancelled and its stock released after this long
    delivery_check: str                         # "none": every address is accepted; another value: a check refuses undeliverable ones
    checkout_available: bool                    # false: checkout would answer 503 (see unavailable_reasons)
    payments_available: bool                    # false: paying would be refused for this currency
    unavailable_reasons: list[str] = []


class StateRead(BaseModel):
    code: str    # the two-digit GST state code (27 = Maharashtra)
    name: str


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
