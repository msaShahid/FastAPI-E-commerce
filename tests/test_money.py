"""Currency metadata and conversion: pure unit tests (no database)."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.money import (
    CURRENCIES,
    CurrencyNotPayableError,
    InvalidMoneyError,
    UnsupportedCurrencyError,
    format_minor,
    from_minor,
    from_stripe_amount,
    get_currency,
    to_minor,
    to_stripe_amount,
)


@pytest.mark.parametrize(("code", "exponent"), [("usd", 2), ("inr", 2), ("eur", 2), ("gbp", 2),
                                                ("jpy", 0), ("krw", 0), ("kwd", 3), ("bhd", 3)])
def test_minor_unit_exponents(code, exponent):
    assert get_currency(code).exponent == exponent


def test_only_currencies_verified_against_stripes_documentation_are_payable():
    assert {c.code for c in CURRENCIES.values() if c.payment_verified} == {"usd", "inr", "eur", "gbp", "jpy"}
    for code in ("kwd", "bhd", "krw"):  # three-decimal and unconfirmed currencies are NOT claimed
        assert get_currency(code).payment_verified is False


@pytest.mark.parametrize("code", ["XYZ", "", "dollars", "us"])
def test_unsupported_currencies_are_rejected(code):
    with pytest.raises(UnsupportedCurrencyError):
        get_currency(code)


def test_codes_are_case_insensitive():
    assert get_currency(" USD ").code == "usd"


@pytest.mark.parametrize(("amount", "code", "minor"), [
    ("10.99", "usd", 1099), ("10.99", "inr", 1099), ("500", "jpy", 500), ("12.345", "kwd", 12345),
    (Decimal("0.5"), "usd", 50), (7, "usd", 700), (7, "jpy", 7), (7, "kwd", 7000),
])
def test_to_minor_uses_the_currency_exponent(amount, code, minor):
    assert to_minor(amount, code) == minor


@pytest.mark.parametrize(("amount", "code", "minor"), [
    ("10.005", "usd", 1001),   # half-up, not banker's rounding
    ("10.004", "usd", 1000),
    ("0.5", "jpy", 1),         # zero-decimal: .5 rounds up
    ("0.4999", "jpy", 0),
    ("1.2345", "kwd", 1235),   # three-decimal
    ("1.2344", "kwd", 1234),
])
def test_rounding_is_explicit_half_up(amount, code, minor):
    assert to_minor(amount, code) == minor


@pytest.mark.parametrize("bad", [1.1, 10.99, True])
def test_floats_and_bools_are_never_accepted_as_money(bad):
    with pytest.raises(InvalidMoneyError):
        to_minor(bad, "usd")


@pytest.mark.parametrize(("minor", "code", "text"), [
    (1099, "usd", "10.99"), (5, "usd", "0.05"), (500, "jpy", "500"), (12345, "kwd", "12.345"), (7, "kwd", "0.007"),
])
def test_from_minor_is_exact_and_formats_with_the_right_digits(minor, code, text):
    assert format_minor(minor, code) == text
    assert to_minor(from_minor(minor, code), code) == minor  # round trip is lossless


@pytest.mark.parametrize("bad", [1.5, True, "5", None])
def test_from_minor_requires_an_integer(bad):
    with pytest.raises(InvalidMoneyError):
        from_minor(bad, "usd")


@pytest.mark.parametrize(("code", "minor"), [("usd", 1099), ("inr", 250000), ("jpy", 500), ("eur", 1), ("gbp", 99)])
def test_stripe_amount_is_the_minor_unit_integer_for_verified_currencies(code, minor):
    assert to_stripe_amount(minor, code) == minor
    assert from_stripe_amount(minor, code) == minor


@pytest.mark.parametrize("code", ["kwd", "bhd", "krw"])
def test_unverified_currencies_are_refused_for_stripe(code):
    with pytest.raises(CurrencyNotPayableError) as exc:
        to_stripe_amount(1000, code)
    assert exc.value.code == "payment_currency_not_verified"


@pytest.mark.parametrize("bad", [0, -5, 1.5, True, "100", None])
def test_invalid_stripe_amounts_are_refused(bad):
    with pytest.raises(InvalidMoneyError):
        to_stripe_amount(bad, "usd")


def _settings(**over):
    base = dict(_env_file=None, database_url="postgresql+asyncpg://x", jwt_secret="x" * 32,
                stripe_secret_key="sk_test_x", stripe_webhook_secret="whsec_x")
    return Settings(**{**base, **over})


def test_payment_currencies_default_to_none_and_only_verified_ones_are_accepted(clean_settings_env):
    assert _settings().enabled_payment_currencies() == set()
    assert _settings(payment_currencies_enabled="INR, usd").enabled_payment_currencies() == {"inr", "usd"}
    with pytest.raises(ValidationError):
        _settings(payment_currencies_enabled="kwd")
    with pytest.raises(ValidationError):
        _settings(payment_currencies_enabled="xyz")
