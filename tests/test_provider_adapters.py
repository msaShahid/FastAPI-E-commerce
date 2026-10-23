"""
Provider adapters and secret storage, as UNIT tests: no database, no network.
  * Stripe: signature checks are real; outbound calls are not made. Razorpay: tested against MOCKED HTTP only
    (httpx.MockTransport). Nothing here is evidence that either adapter works against the real provider.
"""

import hashlib
import hmac
import json

import httpx
import pytest
import stripe
from cryptography.fernet import Fernet
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.core.payments.exceptions import (
    InvalidWebhookPayloadError,
    InvalidWebhookSignatureError,
    PaymentProviderError,
    PaymentProviderTimeoutError,
)
from app.core.payments.razorpay_provider import RazorpayPaymentProvider
from app.core.payments.registry import SPECS
from app.core.payments.stripe_provider import StripePaymentProvider
from app.core.secrets import SecretCipher, SecretUnreadableError, hint, parse_previous_keys

WH = "rzp_whsec_unit_secret"


def rz(handler, webhook_secret=WH) -> RazorpayPaymentProvider:
    return RazorpayPaymentProvider(key_id="rzp_test_UNIT", key_secret="rzp_secret_UNIT", webhook_secret=webhook_secret,
                                   transport=httpx.MockTransport(handler))


def sign(body: bytes, secret=WH) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ------------------------------------------------------------------------------------------ registry / capabilities
def test_capabilities_are_explicit_and_conservative():
    assert {"stripe", "razorpay"} <= set(SPECS)
    assert SPECS["razorpay"].currencies == frozenset({"inr"})              # not assumed to match Stripe's
    assert SPECS["razorpay"].supports_cancel is False and SPECS["stripe"].supports_cancel is True
    assert SPECS["razorpay"].dispute_events is False and SPECS["stripe"].dispute_events is True
    assert "mocked" in SPECS["razorpay"].verification and "not verified" in SPECS["razorpay"].verification
    assert "outbound API calls not verified" in SPECS["stripe"].verification


# ------------------------------------------------------------------------------------------ secret storage
def test_encrypted_secrets_round_trip_and_the_ciphertext_does_not_contain_the_secret():
    cipher = SecretCipher("k1", Fernet.generate_key().decode())
    blob = cipher.encrypt("sk_test_super_secret_value")
    assert "sk_test_super_secret_value" not in blob and blob.startswith("k1:")
    assert cipher.decrypt(blob) == "sk_test_super_secret_value"
    assert cipher.encrypt("x") != cipher.encrypt("x")                           # not deterministic


def test_a_wrong_or_missing_key_is_unreadable_never_a_partial_value():
    blob = SecretCipher("k1", Fernet.generate_key().decode()).encrypt("secret-value")
    with pytest.raises(SecretUnreadableError):
        SecretCipher("k1", Fernet.generate_key().decode()).decrypt(blob)         # same id, different key
    with pytest.raises(SecretUnreadableError):
        SecretCipher("k2", Fernet.generate_key().decode()).decrypt(blob)         # unknown key id


def test_key_rotation_keeps_old_records_readable_while_new_ones_use_the_new_key():
    old_key, new_key = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    blob = SecretCipher("k1", old_key).encrypt("value")
    rotated = SecretCipher("k2", new_key, previous={"k1": old_key})
    assert rotated.decrypt(blob) == "value" and rotated.encrypt("value").startswith("k2:")
    assert rotated.key_id_of(blob) == "k1"


def test_hints_never_reveal_enough_to_use_a_secret():
    assert hint("sk_test_abcdefghij1234") == "...1234" and hint("short") == "...set"
    assert parse_previous_keys("k1:abc, k2:def") == {"k1": "abc", "k2": "def"}


def _settings(**over):
    return Settings(_env_file=None, database_url="postgresql+asyncpg://x", jwt_secret="x" * 32, stripe_secret_key="sk_test_x",
                    stripe_webhook_secret="whsec_x", **over)


def test_an_invalid_encryption_key_is_rejected_at_startup_without_echoing_it(clean_settings_env):
    bad = "this-is-not-a-fernet-key-but-looks-secret"
    with pytest.raises(ValidationError) as exc:
        _settings(provider_secrets_key=bad)
    assert bad not in str(exc.value)
    assert _settings().provider_secrets_key is None                              # unset is allowed: storing credentials is then refused
    good = Fernet.generate_key().decode()
    s = _settings(provider_secrets_key=good)
    assert isinstance(s.provider_secrets_key, SecretStr) and good not in repr(s)  # never printed with the settings


# ------------------------------------------------------------------------------------------ Stripe: per-instance credentials
def test_two_stripe_configurations_never_share_a_key(monkeypatch):
    seen = []
    monkeypatch.setattr(stripe.PaymentIntent, "create", lambda **kw: seen.append(kw["api_key"]) or type("I", (), {"id": "pi_1", "client_secret": "s", "status": "requires_payment_method"})())
    import asyncio

    async def go():
        for key in ("sk_test_AAAA", "sk_live_BBBB"):
            await StripePaymentProvider(secret_key=key, webhook_secret="whsec_x").create_payment_intent(
                amount_cents=100, currency="inr", metadata={}, idempotency_key="k")

    asyncio.run(go())
    assert seen == ["sk_test_AAAA", "sk_live_BBBB"]
    assert stripe.api_key in (None, "") or not str(stripe.api_key).startswith(("sk_test_AAAA", "sk_live_BBBB"))   # nothing global


# ------------------------------------------------------------------------------------------ Razorpay adapter (mocked HTTP)
async def test_create_order_sends_paise_uppercase_currency_and_maps_the_response():
    captured = {}

    def handler(request):
        captured.update(method=request.method, path=request.url.path, body=json.loads(request.content), auth=request.headers["authorization"])
        return httpx.Response(200, json={"id": "order_X1", "status": "created", "amount": 12345, "currency": "INR"})

    result = await rz(handler).create_payment_intent(amount_cents=12345, currency="inr", metadata={"order_id": "o-1"}, idempotency_key="order:abc:intent:0")
    assert (captured["method"], captured["path"]) == ("POST", "/v1/orders")
    assert captured["body"]["amount"] == 12345 and captured["body"]["currency"] == "INR"       # smallest unit, upper-case code
    assert captured["body"]["notes"] == {"order_id": "o-1"} and captured["auth"].startswith("Basic ")
    assert (result.provider_payment_id, result.client_secret, result.status) == ("order_X1", "order_X1", "created")


@pytest.mark.parametrize(("razorpay_status", "shared"), [("paid", "succeeded"), ("created", "requires_payment_method"), ("attempted", "requires_action"), ("weird", "unknown")])
async def test_order_status_is_mapped_to_the_shared_vocabulary(razorpay_status, shared):
    provider = rz(lambda request: httpx.Response(200, json={"id": "order_X1", "status": razorpay_status}))
    assert (await provider.retrieve_payment_intent(provider_payment_id="order_X1")).status == shared


async def test_a_refund_needs_the_payment_reference_and_is_sent_with_an_idempotency_key():
    seen = {}

    def handler(request):
        seen.update(path=request.url.path, body=json.loads(request.content), idem=request.headers.get("x-refund-idempotency"))
        return httpx.Response(200, json={"id": "rfnd_1", "amount": 2000, "status": "processed", "currency": "INR"})

    provider = rz(handler)
    with pytest.raises(PaymentProviderError):                                                  # an ORDER id is not a payment reference
        await provider.create_refund(provider_payment_id="order_X1", amount_cents=2000, currency="inr", idempotency_key="k")
    result = await provider.create_refund(provider_payment_id="pay_P1", amount_cents=2000, currency="inr", idempotency_key="refund:abc:0")
    assert seen["path"] == "/v1/payments/pay_P1/refund" and seen["body"]["amount"] == 2000 and seen["idem"] == "refund:abc:0"
    assert (result.provider_refund_id, result.amount_cents, result.status, result.currency) == ("rfnd_1", 2000, "succeeded", "inr")


async def test_a_pending_or_failed_refund_status_is_mapped_not_assumed_successful():
    for status, expected in (("pending", "pending"), ("failed", "failed"), ("mystery", "pending")):
        provider = rz(lambda request, s=status: httpx.Response(200, json={"id": "rfnd_1", "amount": 100, "status": s}))
        assert (await provider.create_refund(provider_payment_id="pay_P1", amount_cents=100, currency="inr", idempotency_key="k")).status == expected


async def test_orders_cannot_be_cancelled_and_the_adapter_says_so():
    provider = rz(lambda request: httpx.Response(200, json={}))
    assert provider.supports_cancel is False
    with pytest.raises(PaymentProviderError):
        await provider.cancel_payment_intent(provider_payment_id="order_X1", idempotency_key="k")


async def test_timeouts_and_errors_are_classified_without_leaking_the_response_body():
    def timeout(request):
        raise httpx.ConnectTimeout("slow")

    with pytest.raises(PaymentProviderTimeoutError):
        await rz(timeout).create_payment_intent(amount_cents=1, currency="inr", metadata={}, idempotency_key="k")
    leaky = rz(lambda request: httpx.Response(400, json={"error": {"description": "bad key rzp_secret_UNIT in request"}}))
    with pytest.raises(PaymentProviderError) as exc:
        await leaky.create_payment_intent(amount_cents=1, currency="inr", metadata={}, idempotency_key="k")
    assert "rzp_secret_UNIT" not in str(exc.value)


async def test_credential_validation_distinguishes_bad_credentials_from_an_outage():
    assert (await rz(lambda r: httpx.Response(200, json={"items": []})).validate_credentials()).valid is True
    assert (await rz(lambda r: httpx.Response(200, json={"items": []})).validate_credentials()).detected_mode == "test"
    bad = await rz(lambda r: httpx.Response(401, json={})).validate_credentials()
    assert (bad.valid, bad.reason) == (False, "authentication_failed")
    down = await rz(lambda r: httpx.Response(503, json={})).validate_credentials()
    assert (down.valid, down.reason) == (False, "provider_unreachable_or_restricted")


def captured_body(order_id="order_X1", pay="pay_P1", amount=2000):
    return json.dumps({"event": "payment.captured", "payload": {"payment": {"entity": {
        "id": pay, "order_id": order_id, "amount": amount, "currency": "INR", "status": "captured"}}}}).encode()


def test_a_correctly_signed_capture_is_normalised_to_the_shared_event_with_both_references():
    body = captured_body()
    event = rz(lambda r: None).verify_and_parse_webhook(payload=body, signature_header=sign(body), headers={"x-razorpay-event-id": "evt_rz_1"})
    assert (event.event_id, event.event_type, event.provider, event.provider_payment_id, event.provider_reference) == (
        "evt_rz_1", "payment_intent.succeeded", "razorpay", "order_X1", "pay_P1")
    assert (event.amount, event.amount_received, event.currency) == (2000, 2000, "inr")


def test_without_an_event_id_header_the_event_id_is_a_stable_hash_of_the_raw_body():
    body = captured_body()
    a = rz(lambda r: None).verify_and_parse_webhook(payload=body, signature_header=sign(body), headers={})
    b = rz(lambda r: None).verify_and_parse_webhook(payload=body, signature_header=sign(body))
    assert a.event_id == b.event_id == hashlib.sha256(body).hexdigest()


@pytest.mark.parametrize("tamper", ["wrong_secret", "changed_byte", "reserialized", "empty", "garbage", "uppercase_ok"])
def test_only_the_exact_signed_bytes_verify(tamper):
    body = captured_body()
    header, payload = sign(body), body
    if tamper == "wrong_secret":
        header = sign(body, "other-secret-value")
    elif tamper == "changed_byte":
        payload = body.replace(b"2000", b"2001")
    elif tamper == "reserialized":
        payload = json.dumps(json.loads(body), indent=2).encode()
    elif tamper == "empty":
        header = ""
    elif tamper == "garbage":
        header = "not-a-signature"
    elif tamper == "uppercase_ok":                                   # hex is case-insensitive: still the same signature
        header = header.upper()
        assert rz(lambda r: None).verify_and_parse_webhook(payload=payload, signature_header=header).provider == "razorpay"
        return
    with pytest.raises(InvalidWebhookSignatureError):
        rz(lambda r: None).verify_and_parse_webhook(payload=payload, signature_header=header)


def test_a_provider_without_a_webhook_secret_accepts_nothing():
    body = captured_body()
    with pytest.raises(InvalidWebhookSignatureError):
        rz(lambda r: None, webhook_secret=None).verify_and_parse_webhook(payload=body, signature_header=sign(body))


@pytest.mark.parametrize("body", [b"not json", b"[]", b"{}", b'{"event": "payment.captured"}', b'{"event": "payment.captured", "payload": {"payment": {"entity": {}}}}'])
def test_signed_but_malformed_events_are_refused_as_a_payload_error(body):
    with pytest.raises(InvalidWebhookPayloadError):
        rz(lambda r: None).verify_and_parse_webhook(payload=body, signature_header=sign(body))


def test_refund_events_and_unsupported_events_are_normalised_or_kept_for_review():
    processed = json.dumps({"event": "refund.processed", "payload": {"refund": {"entity": {
        "id": "rfnd_9", "payment_id": "pay_P1", "amount": 2000, "currency": "INR"}}}}).encode()
    refund = rz(lambda r: None).verify_and_parse_webhook(payload=processed, signature_header=sign(processed), headers={"x-razorpay-event-id": "e2"})
    assert (refund.event_type, refund.refund_status, refund.provider_refund_id, refund.provider_reference, refund.provider_payment_id) == (
        "refund.updated", "succeeded", "rfnd_9", "pay_P1", "")
    other = json.dumps({"event": "settlement.processed", "payload": {}}).encode()
    unknown = rz(lambda r: None).verify_and_parse_webhook(payload=other, signature_header=sign(other), headers={"x-razorpay-event-id": "e3"})
    assert unknown.event_type == "razorpay.settlement.processed"                  # not an event we act on: recorded, never dropped
