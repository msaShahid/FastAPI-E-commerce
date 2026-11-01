"""The built-in demo gateway's pure behaviour: signed events, the published test details, and the refusal of anything else."""

import json
import time

import pytest

from app.core.payments.demo_provider import (
    TEST_CARDS,
    DemoPaymentProvider,
    build_event,
    normalise_card,
    sign,
)
from app.core.payments.exceptions import InvalidWebhookPayloadError, InvalidWebhookSignatureError
from app.modules.payments.exceptions.demo_exceptions import DemoOnlyTestDetailsError
from app.modules.payments.schemas.demo import DemoPayRequest
from app.modules.payments.services.demo_checkout import published_test_details, resolve_outcome

SECRET = "demo_whsec_unit_test_secret"
ORDER = "2a6bbf0c-1c4e-4a5c-8f6e-0c6f3c8f9a11"


def event(**over):
    base = dict(event_id="evt_demo_1", event_type="payment_intent.succeeded", provider_payment_id="pi_demo_x", amount_cents=12300, currency="USD")
    return build_event(**{**base, **over})


def provider(secret=SECRET):
    return DemoPaymentProvider(webhook_secret=secret)


def test_a_correctly_signed_event_is_parsed_with_everything_the_payment_checks_need():
    payload = event()
    parsed = provider().verify_and_parse_webhook(payload=payload, signature_header=sign(SECRET, payload))
    assert (parsed.event_id, parsed.event_type, parsed.provider_payment_id, parsed.amount, parsed.amount_received, parsed.currency, parsed.provider) == (
        "evt_demo_1", "payment_intent.succeeded", "pi_demo_x", 12300, 12300, "usd", "demo")


def test_a_failed_event_reports_nothing_received():
    payload = event(event_type="payment_intent.payment_failed")
    parsed = provider().verify_and_parse_webhook(payload=payload, signature_header=sign(SECRET, payload))
    assert parsed.amount_received == 0 and parsed.event_type == "payment_intent.payment_failed"


@pytest.mark.parametrize("tamper", ["body", "secret", "format", "old", "missing_v1", "empty"])
def test_a_forged_tampered_stale_or_malformed_signature_is_refused(tamper):
    payload = event()
    good = sign(SECRET, payload)
    if tamper == "body":
        payload, header = event(amount_cents=1), good                           # a different body under a genuine signature
    elif tamper == "secret":
        header = sign("another_secret_entirely", payload)
    elif tamper == "format":
        header = "not a signature"
    elif tamper == "old":
        header = sign(SECRET, payload, timestamp=int(time.time()) - 3600)         # genuine but an hour old: a replay
    elif tamper == "missing_v1":
        header = f"t={int(time.time())}"
    else:
        header = ""
    with pytest.raises(InvalidWebhookSignatureError):
        provider().verify_and_parse_webhook(payload=payload, signature_header=header)


def test_a_provider_with_no_secret_verifies_nothing():
    payload = event()
    with pytest.raises(InvalidWebhookSignatureError):
        DemoPaymentProvider(webhook_secret=None).verify_and_parse_webhook(payload=payload, signature_header=sign(SECRET, payload))


def test_a_signed_body_that_is_not_an_event_is_a_payload_error_not_a_crash():
    payload = json.dumps({"hello": "world"}).encode()
    with pytest.raises(InvalidWebhookPayloadError):
        provider().verify_and_parse_webhook(payload=payload, signature_header=sign(SECRET, payload))


def req(**kw):
    return DemoPayRequest(order_id=ORDER, **kw)


@pytest.mark.parametrize(("number", "succeeds", "code"), [
    ("4242 4242 4242 4242", True, None), ("4242424242424242", True, None), ("4242-4242-4242-4242", True, None), ("5555 5555 5555 4444", True, None),
    ("4000 0000 0000 0002", False, "card_declined"), ("4000000000009995", False, "insufficient_funds"),
    ("4000000000000069", False, "expired_card"), ("4000000000000119", False, "processing_error"),
])
def test_the_published_test_cards_have_their_documented_outcomes(number, succeeds, code):
    outcome = resolve_outcome(req(method="card", card_number=number))
    assert (outcome.succeeds, outcome.failure_code) == (succeeds, code)


@pytest.mark.parametrize("number", [
    "4111111111111111", "5105105105105100", "378282246310005", "6011111111111117", "4012888888881881", "1234567812345678", "0000000000000000",
    "4242424242424241", "42424242424242424", "4242", "abcd", " ", "4242 4242 4242 4242 99",
])
def test_anything_that_is_not_a_published_test_card_is_refused_whatever_it_looks_like(number):
    with pytest.raises(DemoOnlyTestDetailsError) as caught:
        resolve_outcome(req(method="card", card_number=number))
    message = str(caught.value)
    assert number.strip() == "" or number not in message                  # the refusal never echoes what was typed
    assert normalise_card(number) == "" or normalise_card(number) not in message


@pytest.mark.parametrize(("method", "kw", "succeeds"), [
    ("upi", {"upi_id": "success@demo"}, True), ("upi", {"upi_id": " SUCCESS@demo "}, True), ("upi", {"upi_id": "failure@demo"}, False),
    ("netbanking", {"bank": "demo_bank_success"}, True), ("netbanking", {"bank": "demo_bank_failure"}, False),
])
def test_upi_ids_and_banks_work_the_same_way(method, kw, succeeds):
    assert resolve_outcome(req(method=method, **kw)).succeeds is succeeds


@pytest.mark.parametrize(("method", "kw"), [("upi", {"upi_id": "someone@okbank"}), ("netbanking", {"bank": "hdfc"}), ("upi", {"upi_id": "9876543210@upi"})])
def test_real_upi_ids_and_banks_are_refused_too(method, kw):
    with pytest.raises(DemoOnlyTestDetailsError):
        resolve_outcome(req(method=method, **kw))


@pytest.mark.parametrize("kw", [{"method": "card"}, {"method": "card", "upi_id": "success@demo"}, {"method": "upi", "card_number": "4242424242424242"},
                                {"method": "card", "card_number": "4242424242424242", "bank": "demo_bank_success"}])
def test_only_the_detail_of_the_chosen_method_is_accepted(kw):
    with pytest.raises(ValueError):
        req(**kw)


def test_expiry_cvc_and_name_are_not_accepted_at_all():
    for extra in ("cvc", "cvv", "expiry", "exp_month", "name", "card_holder"):
        with pytest.raises(ValueError):
            req(method="card", card_number="4242424242424242", **{extra: "123"})


def test_the_published_details_match_what_the_gateway_accepts_and_say_it_is_a_demo():
    details = published_test_details(True)
    assert details.demo_active is True and "no real money" in details.notice.lower()
    assert {c.value.replace(" ", "") for c in details.cards} == set(TEST_CARDS)
    assert {c.outcome for c in details.cards} == {"succeeds", "declined"} and details.upi_ids and details.banks
    for card in details.cards:                                                        # every published detail really is accepted
        assert resolve_outcome(req(method="card", card_number=card.value)).succeeds == (card.outcome == "succeeds")
