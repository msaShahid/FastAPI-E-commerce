"""
Currency metadata and money conversion. All money in this application is an INTEGER number of
MINOR units of a currency (the `*_cents` names are historical: for JPY a "cent" is one yen,
for a three-decimal currency it is 1/1000). Decimal arithmetic is used wherever a fraction can
appear, floats never.

Where the facts come from (do not extend this table without checking the same sources):
  * exponent: ISO 4217 minor-unit exponent.
  * payment_verified: confirmed against Stripe's official currencies page
    (https://docs.stripe.com/currencies, checked 2026-10-18): amounts are integers in the
    currency's minor unit, two-decimal is the default, JPY is zero-decimal. The page does NOT
    document three-decimal currencies or confirm KRW, so those are NOT marked verified and
    payments in them are refused. Which verified currencies a given Stripe ACCOUNT can actually
    charge is account-specific and is declared by the client in PAYMENT_CURRENCIES_ENABLED.

Rounding is explicit: MONEY_ROUNDING (half-up) whenever a Decimal is converted to minor units.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.core.exceptions import InvalidStateError, ServiceUnavailableError

MONEY_ROUNDING = ROUND_HALF_UP


@dataclass(frozen=True)
class Currency:
    code: str  # lower-case ISO 4217 (the form Stripe expects)
    exponent: int  # digits after the decimal point
    payment_verified: bool


CURRENCIES: dict[str, Currency] = {
    c.code: c
    for c in (
        Currency("usd", 2, True),
        Currency("inr", 2, True),
        Currency("eur", 2, True),
        Currency("gbp", 2, True),
        Currency("jpy", 0, True),
        Currency("krw", 0, False),  # exponent per ISO 4217; not confirmed on Stripe's page
        Currency("kwd", 3, False),  # three-decimal: arithmetic supported, payments not verified
        Currency("bhd", 3, False),
    )
}


# ------------------------------------------------------------------ errors
class UnsupportedCurrencyError(InvalidStateError):
    code = "currency_unsupported"

    def __init__(self, code: str) -> None:
        super().__init__(f"Currency '{code}' is not supported by this platform")


class StoreCurrencyNotConfiguredError(ServiceUnavailableError):
    code = "store_currency_not_configured"

    def __init__(self) -> None:
        super().__init__("The store currency is not configured")


class CurrencyNotPayableError(ServiceUnavailableError):
    """The currency exists but payments in it are not verified or not enabled for this account."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"Payments in '{code}' are not available ({reason})")
        self.code = reason


class InvalidMoneyError(InvalidStateError):
    code = "invalid_money_amount"


# ------------------------------------------------------------------ helpers
def normalize_code(code: str) -> str:
    if not isinstance(code, str):
        raise UnsupportedCurrencyError(str(code))
    return code.strip().lower()


def get_currency(code: str) -> Currency:
    normalized = normalize_code(code)
    try:
        return CURRENCIES[normalized]
    except KeyError:
        raise UnsupportedCurrencyError(normalized) from None


def to_minor(amount: Decimal | int | str, code: str) -> int:
    """A decimal amount in major units (10.99) -> integer minor units, rounded half-up. Never a float."""
    if isinstance(amount, (bool, float)):
        raise InvalidMoneyError("Binary floating point is not allowed for money")
    exponent = get_currency(code).exponent
    value = Decimal(str(amount)) if not isinstance(amount, Decimal) else amount
    return int((value * (Decimal(10) ** exponent)).quantize(Decimal(1), rounding=MONEY_ROUNDING))


def from_minor(minor: int, code: str) -> Decimal:
    """Integer minor units -> exact Decimal in major units (1099 usd -> Decimal('10.99'))."""
    if isinstance(minor, bool) or not isinstance(minor, int):
        raise InvalidMoneyError("Minor-unit amounts must be integers")
    exponent = get_currency(code).exponent
    return (Decimal(minor) / (Decimal(10) ** exponent)).quantize(Decimal(1).scaleb(-exponent))


def format_minor(minor: int, code: str) -> str:
    return f"{from_minor(minor, code):f}"


def to_stripe_amount(minor: int, code: str) -> int:
    """The integer Stripe expects. Refuses currencies not verified for payments and invalid amounts.

    For every verified currency Stripe's amount IS the minor-unit integer (no multiplication),
    so this is the single, explicit place where that rule lives.
    """
    currency = get_currency(code)
    if not currency.payment_verified:
        raise CurrencyNotPayableError(currency.code, "payment_currency_not_verified")
    if isinstance(minor, bool) or not isinstance(minor, int) or minor <= 0:
        raise InvalidMoneyError("A payment amount must be a positive integer in minor units")
    return minor


def from_stripe_amount(amount: int, code: str) -> int:
    """Stripe's integer back to our minor units (the inverse of to_stripe_amount)."""
    get_currency(code)
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise InvalidMoneyError("Stripe amounts are integers")
    return amount
