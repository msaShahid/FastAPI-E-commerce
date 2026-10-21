"""The Stripe test-mode verification script must be impossible to run against live keys or by accident."""

import pathlib

import pytest

from scripts.stripe_testmode_check import guard_errors

OK = {"STRIPE_SECRET_KEY": "sk_test_abc123", "STRIPE_TESTMODE_VERIFY": "yes", "LIVE_PAYMENTS_ENABLED": "false"}


def test_a_proper_test_key_with_explicit_confirmation_is_allowed():
    assert guard_errors(OK) == []


@pytest.mark.parametrize("key", ["sk_live_abc", "rk_live_abc", "pk_test_abc", "", "sk_test_REPLACE_WITH_YOUR_STRIPE_TEST_KEY"])
def test_live_restricted_publishable_and_placeholder_keys_are_refused(key):
    assert guard_errors({**OK, "STRIPE_SECRET_KEY": key})


@pytest.mark.parametrize("flag", ["", "no", "true", "1"])
def test_it_never_runs_without_explicit_confirmation(flag):
    assert guard_errors({**OK, "STRIPE_TESTMODE_VERIFY": flag})


@pytest.mark.parametrize("value", ["true", "1", "yes", "on"])
def test_it_refuses_when_live_payments_are_enabled(value):
    assert guard_errors({**OK, "LIVE_PAYMENTS_ENABLED": value})


def test_the_script_never_prints_the_secret_key():
    source = pathlib.Path("scripts/stripe_testmode_check.py").read_text(encoding="utf-8")
    assert "print(settings.stripe_secret_key" not in source and "print(key" not in source
    assert "STRIPE_SECRET_KEY\"]" not in source.replace("env.get(\"STRIPE_SECRET_KEY\"", "")   # only read through the guard
