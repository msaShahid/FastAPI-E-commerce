"""H1: staging/production must refuse template or guessable secrets (no database or network needed)."""

import hashlib
import hmac
import json
import time

import pytest

from app.core.config import ConfigurationError, Settings

GOOD = {
    "database_url": "postgresql+asyncpg://u:p@db/x",
    "jwt_secret": "9f2c4a7e1b8d3c6a5e0f7b2d4c9a1e8f6b3d5c7a9e1f2b4d6c8a0e3f5b7d9c1a",
    "stripe_secret_key": "sk_test_51Nabcdefghijklmnopqrstuv",
    "stripe_webhook_secret": "whsec_8d3f1a6b9c2e5d7f0a4b8c1e3d6f9a2b",
}


def build(**over):
    return Settings(_env_file=None, **{**GOOD, **over})


@pytest.mark.parametrize("env", ["production", "staging"])
def test_real_looking_secrets_start_in_hardened_environments(env):
    assert build(environment=env).environment == env


@pytest.mark.parametrize("env", ["production", "staging"])
@pytest.mark.parametrize(
    "over",
    [
        {"jwt_secret": "CHANGE_ME_generate_a_new_64_hex_character_secret"},  # the shipped .env.production value
        {"jwt_secret": "a" * 64},  # long but degenerate
        {"jwt_secret": "short-but-random-1x9Qz"},  # too short
        {"stripe_webhook_secret": "whsec_CHANGE_ME"},  # the shipped value
        {"stripe_webhook_secret": ""},
        {"stripe_webhook_secret": "8d3f1a6b9c2e5d7f0a4b8c1e3d6f9a2b"},  # missing whsec_ prefix
        {"stripe_webhook_secret": "whsec_ci"},
        {"stripe_secret_key": "sk_test_CHANGE_ME"},  # the shipped value
        {"stripe_secret_key": "not-a-stripe-key-0123456789"},
    ],
)
def test_placeholder_or_weak_secrets_are_refused_in_hardened_environments(env, over):
    with pytest.raises(ConfigurationError) as exc:
        build(environment=env, **over)
    message = str(exc.value)
    for value in over.values():
        if value:
            assert value not in message  # the offending value is never echoed


def test_development_and_testing_keep_their_throwaway_values():
    for env in ("development", "testing"):
        assert build(environment=env, jwt_secret="x", stripe_webhook_secret="whsec_ci", stripe_secret_key="sk_test_ci")


@pytest.mark.parametrize("env", ["prod", "Production", "", "live"])
def test_unknown_environment_labels_cannot_bypass_the_checks(env):
    with pytest.raises(ConfigurationError):
        build(environment=env, jwt_secret="x")


@pytest.mark.parametrize("alg", ["none", "RS256", "hs256"])
def test_only_hmac_jwt_algorithms_are_accepted(alg):
    with pytest.raises(ConfigurationError):
        build(jwt_algorithm=alg)


def test_a_forged_webhook_signed_with_the_old_placeholder_cannot_reach_a_hardened_deployment():
    """End to end for H1: the placeholder that used to verify a forged event now stops the app from configuring at all."""
    from app.core.payments.stripe_provider import StripePaymentProvider

    placeholder = "whsec_CHANGE_ME"
    body = json.dumps({"id": "evt_forged", "type": "payment_intent.succeeded",
                       "data": {"object": {"id": "pi_known", "amount": 50000, "currency": "inr"}}}).encode()
    ts = str(int(time.time()))
    sig = hmac.new(placeholder.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    # Baseline: against a provider that WAS configured with the placeholder the forgery verifies ...
    assert StripePaymentProvider(secret_key="sk_test_x", webhook_secret=placeholder).verify_and_parse_webhook(
        payload=body, signature_header=f"t={ts},v1={sig}").provider_payment_id == "pi_known"
    # ... which is why such a configuration can no longer start in production.
    with pytest.raises(ConfigurationError):
        build(environment="production", stripe_webhook_secret=placeholder)
