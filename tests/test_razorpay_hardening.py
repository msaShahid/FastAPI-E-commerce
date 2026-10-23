"""M3 (adapter level, mocked HTTP only - NOT a Razorpay sandbox check): replay identity and receipts."""

import hashlib
import json

import httpx
from test_provider_adapters import captured_body, rz, sign

from app.core.payments.razorpay_provider import RECEIPT_MAX_LENGTH, receipt_for


def test_the_event_id_header_cannot_change_a_signed_events_identity():
    body = captured_body()
    ids = {
        rz(lambda r: None).verify_and_parse_webhook(
            payload=body, signature_header=sign(body), headers={"x-razorpay-event-id": claimed}).event_id
        for claimed in ("evt_original", "evt_changed_by_a_replaying_party", "", "x" * 500)
    }
    assert ids == {hashlib.sha256(body).hexdigest()}  # one signed body, one identity, whatever the unsigned header claims


def test_different_signed_bodies_are_different_events():
    a, b = captured_body(pay="pay_A"), captured_body(pay="pay_B")
    ids = [rz(lambda r: None).verify_and_parse_webhook(payload=x, signature_header=sign(x)).event_id for x in (a, b)]
    assert ids[0] != ids[1]


def test_receipts_fit_the_limit_and_stay_unique_per_attempt():
    order = "42a8b91a-4b5e-4e88-ab92-3576569d906a"
    keys = [f"order:{order}:intent:{n}" for n in range(12)]
    receipts = [receipt_for(k) for k in keys]
    assert all(len(r) <= RECEIPT_MAX_LENGTH for r in receipts)
    assert len(set(receipts)) == len(keys)  # the old key[:40] made every attempt for an order identical
    assert {k[:40] for k in keys} == {keys[0][:40]}  # ...which this documents
    assert receipt_for(keys[0]) == receipts[0]  # deterministic: a retry of the same attempt sends the same receipt


async def test_the_receipts_actually_sent_for_orders_and_refunds_are_the_compact_unique_form():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.url.path, json.loads(request.content), dict(request.headers)))
        if request.url.path.endswith("/refund"):
            return httpx.Response(200, json={"id": "rfnd_1", "amount": 2000, "status": "processed", "currency": "INR"})
        return httpx.Response(200, json={"id": "order_1", "status": "created"})

    provider = rz(handler)
    key0, key1 = "order:42a8b91a-4b5e-4e88-ab92-3576569d906a:intent:0", "order:42a8b91a-4b5e-4e88-ab92-3576569d906a:intent:1"
    for key in (key0, key1):
        await provider.create_payment_intent(amount_cents=2000, currency="inr", metadata={}, idempotency_key=key)
    await provider.create_refund(provider_payment_id="pay_1", amount_cents=2000, currency="inr", idempotency_key="refund:abc:0")
    order_receipts = [body["receipt"] for path, body, _ in sent if path.endswith("/orders")]
    assert order_receipts == [receipt_for(key0), receipt_for(key1)] and order_receipts[0] != order_receipts[1]
    path, refund_body, headers = sent[-1]
    assert refund_body["receipt"] == receipt_for("refund:abc:0") and len(refund_body["receipt"]) <= RECEIPT_MAX_LENGTH
    assert headers["x-refund-idempotency"] == "refund:abc:0"  # the full key still travels in the idempotency header
