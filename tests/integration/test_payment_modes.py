# ruff: noqa: F811  (fixtures imported from other suites are re-declared as test parameters)
"""The store's payment mode from the admin panel: the demo gateway end to end, test and live gateways (faked), the gateway check, live
activation and deactivation, webhook security, and that demo money never counts as a sale."""

import asyncio
import json
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import text
from test_cart_variants import add, signup
from test_invoices import fulfil
from test_order_payment_lifecycle import category, jobs  # noqa: F401
from test_variant_admin import make_product
from test_variant_checkout import checkout

from app.core.config import get_settings
from app.core.payments.demo_provider import build_event, sign
from app.core.payments.exceptions import PaymentProviderError
from app.core.payments.interface import (
    CredentialCheck,
    PaymentIntentResult,
    PaymentProvider,
    RefundResult,
)
from app.core.payments.registry import SPECS
from app.core.secrets import get_cipher

API = "/api/v1"
ADDR = {"recipient_name": "R", "line1": "1 Test St", "city": "C", "state": "MH", "postal_code": "400001", "country": "IN"}
LIVE_SECRET = "sk_live_DISTINCTIVE_SECRET_VALUE_4242"
LIVE_WEBHOOK = "whsec_DISTINCTIVE_WEBHOOK_VALUE_4242"


class World:
    def __init__(self) -> None:
        self.fail_create = False
        self.created: list = []


class FakeStripe(PaymentProvider):
    name = "stripe"

    def __init__(self, world, secret_key, webhook_secret) -> None:
        self.w, self._k = world, secret_key

    async def validate_credentials(self):
        return CredentialCheck(valid=True, detected_mode="live" if self._k.startswith("sk_live_") else "test")

    async def create_payment_intent(self, *, amount_cents, currency, metadata, idempotency_key):
        if self.w.fail_create:
            raise PaymentProviderError("boom")
        self.w.created.append(idempotency_key)
        return PaymentIntentResult(f"pi_fake_{len(self.w.created)}", "cs_fake", "requires_payment_method")

    async def cancel_payment_intent(self, *, provider_payment_id, idempotency_key):
        return PaymentIntentResult(provider_payment_id, "", "canceled")

    async def retrieve_payment_intent(self, *, provider_payment_id):
        return PaymentIntentResult(provider_payment_id, "", "requires_payment_method")

    def verify_and_parse_webhook(self, *, payload, signature_header):
        raise NotImplementedError

    async def create_refund(self, *, provider_payment_id, amount_cents, currency, idempotency_key):
        return RefundResult("re_fake", amount_cents, "succeeded", currency)


@pytest.fixture
def world(monkeypatch):
    w = World()
    monkeypatch.setattr(get_settings(), "provider_secrets_key", SecretStr(Fernet.generate_key().decode()))
    monkeypatch.setitem(SPECS, "stripe", replace(SPECS["stripe"], factory=lambda c, wh: FakeStripe(w, c["secret_key"], wh)))
    return w


async def overview(client, admin_headers):
    r = await client.get(f"{API}/admin/payments/overview", headers=admin_headers)
    assert r.status_code == 200, r.text
    return r.json()


async def enable_demo(client, admin_headers):
    r = await client.post(f"{API}/admin/payments/demo/enable", json={"confirm_no_real_money": True}, headers=admin_headers)
    assert r.status_code == 200, r.text
    return r.json()


async def pending_order(client, admin_headers, category, *, price=5000):
    product = await make_product(client, admin_headers, category, stock=20, price=price, status="active")
    headers, _ = await signup(client)
    await add(client, headers, product_id=product["id"], quantity=1)
    order = (await checkout(client, headers, address=ADDR)).json()
    return headers, order, product


async def start_payment(client, headers, order_id):
    r = await client.post(f"{API}/payments", json={"order_id": order_id}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


async def demo_pay(client, headers, order_id, number="4242 4242 4242 4242", **extra):
    return await client.post(f"{API}/payments/demo/pay", json={"order_id": order_id, "method": "card", "card_number": number, **extra}, headers=headers)


# ============================================================================================== the demo gateway, end to end
async def test_demo_mode_is_switched_on_from_the_admin_panel_with_no_credentials(client, admin_headers, world):
    start = await overview(client, admin_headers)
    assert (start["payment_mode"], start["payments_open"]) == ("none", False) and any(g["provider"] == "demo" for g in start["to_set_up"])
    customer, _ = await signup(client)
    assert (await client.post(f"{API}/admin/payments/demo/enable", json={"confirm_no_real_money": True}, headers=customer)).status_code == 403
    unconfirmed = await client.post(f"{API}/admin/payments/demo/enable", json={"confirm_no_real_money": False}, headers=admin_headers)
    assert unconfirmed.status_code == 422 and unconfirmed.json()["code"] == "confirmation_required"
    after = await enable_demo(client, admin_headers)
    assert (after["payment_mode"], after["payments_open"]) == ("demo", True) and "no real money" in after["message"].lower()
    gateway = after["preferred"]
    assert (gateway["provider"], gateway["is_demo"], gateway["mode"], gateway["status"], gateway["connection"]) == ("demo", True, "test", "enabled", "credentials_ok")
    assert any("DEMO MODE" in w for w in after["warnings"])
    assert (await enable_demo(client, admin_headers))["gateways"][0]["id"] == gateway["id"]                              # asking again changes nothing
    public = (await client.get(f"{API}/store/settings")).json()
    assert (public["payment_mode"], public["payment_demo"]) == ("demo", True)                                             # a storefront can show the banner
    details = (await client.get(f"{API}/payments/demo/test-details")).json()
    assert details["demo_active"] is True and any(c["outcome"] == "declined" for c in details["cards"])
    catalog = (await client.get(f"{API}/admin/payment-providers/catalog", headers=admin_headers)).json()
    assert next(e for e in catalog if e["provider"] == "demo")["modes"] == ["test"]
    live_demo = await client.post(f"{API}/admin/payment-providers", json={"provider": "demo", "mode": "live", "credentials": {}}, headers=admin_headers)
    assert live_demo.status_code == 422 and live_demo.json()["code"] == "use_demo_endpoint"


async def test_a_customer_pays_with_fake_details_and_the_order_is_paid_and_marked_as_demo(client, admin_headers, category, world):
    await enable_demo(client, admin_headers)
    headers, order, product = await pending_order(client, admin_headers, category)
    payment = await start_payment(client, headers, order["id"])
    assert payment["provider"] == "demo" and payment["demo"] is True and payment["client_secret"].startswith("demo_secret_")
    assert (await client.get(f"{API}/orders/{order['id']}", headers=headers)).json()["status"] == "pending"
    paid = await demo_pay(client, headers, order["id"])
    assert paid.status_code == 200, paid.text
    body = paid.json()
    assert (body["demo"], body["succeeded"], body["payment_status"], body["order_status"]) == (True, True, "succeeded", "paid") and body["failure_code"] is None
    got = (await client.get(f"{API}/orders/{order['id']}", headers=headers)).json()
    assert (got["status"], got["demo_payment"]) == ("paid", True)                                                           # identifiable everywhere
    again = await demo_pay(client, headers, order["id"])                                                                    # paying twice changes nothing
    assert again.status_code == 200 and again.json()["succeeded"] is True
    assert (await client.post(f"{API}/payments", json={"order_id": order["id"]}, headers=headers)).status_code != 201       # no second payment
    config = (await client.get(f"{API}/admin/payment-providers", headers=admin_headers)).json()[0]
    assert config["webhook"]["status"] == "verified"                                                                       # the signed event really went through verification


async def test_a_declined_test_card_leaves_the_order_unpaid_and_a_retry_can_succeed(client, admin_headers, category, world):
    await enable_demo(client, admin_headers)
    headers, order, _ = await pending_order(client, admin_headers, category)
    await start_payment(client, headers, order["id"])
    expected = {"4000000000000002": "card_declined", "4000000000009995": "insufficient_funds", "4000000000000069": "expired_card", "4000000000000119": "processing_error"}
    for number, code in expected.items():
        r = (await demo_pay(client, headers, order["id"], number)).json()
        assert (r["succeeded"], r["failure_code"], r["order_status"], r["payment_status"]) == (False, code, "pending", "failed")
    ok = (await demo_pay(client, headers, order["id"], "5555555555554444")).json()
    assert ok["succeeded"] is True and ok["order_status"] == "paid"


async def test_upi_and_netbanking_test_details_work_too(client, admin_headers, category, world):
    await enable_demo(client, admin_headers)
    headers, order, _ = await pending_order(client, admin_headers, category)
    await start_payment(client, headers, order["id"])
    bad = await client.post(f"{API}/payments/demo/pay", json={"order_id": order["id"], "method": "upi", "upi_id": "failure@demo"}, headers=headers)
    assert bad.json()["failure_code"] == "upi_declined"
    bank = await client.post(f"{API}/payments/demo/pay", json={"order_id": order["id"], "method": "netbanking", "bank": "demo_bank_success"}, headers=headers)
    assert bank.json()["succeeded"] is True


async def test_real_card_details_are_refused_never_stored_and_never_echoed(client, admin_headers, category, world, db_session):
    await enable_demo(client, admin_headers)
    headers, order, _ = await pending_order(client, admin_headers, category)
    await start_payment(client, headers, order["id"])
    real = "4111 1111 1111 1111"
    refused = await demo_pay(client, headers, order["id"], real)
    assert refused.status_code == 422 and refused.json()["code"] == "demo_only_test_details"
    assert "4111" not in refused.text and "1111" not in refused.text                                                       # not echoed
    for extra in ({"cvc": "123"}, {"expiry": "12/30"}, {"name": "A Person"}):
        r = await demo_pay(client, headers, order["id"], "4242424242424242", **extra)
        assert r.status_code == 422 and "4242" not in r.text                                                               # not accepted, not echoed
    assert (await client.get(f"{API}/orders/{order['id']}", headers=headers)).json()["status"] == "pending"               # nothing happened
    await db_session.rollback()
    for table in ("payments", "demo_payment_intents", "processed_webhook_events", "deferred_webhook_events", "audit_logs"):                                    # and the number is nowhere
        dump = (await db_session.execute(text(f"SELECT coalesce(string_agg(t::text, ' '), '') FROM {table} t"))).scalar_one()
        assert "4111" not in dump and "1111 1111" not in dump, table


async def test_only_the_customer_who_owns_the_order_can_pay_it_and_a_guest_needs_the_orders_cookie(client, admin_headers, category, world):
    await enable_demo(client, admin_headers)
    headers, order, product = await pending_order(client, admin_headers, category)
    await start_payment(client, headers, order["id"])
    stranger, _ = await signup(client)
    assert (await demo_pay(client, stranger, order["id"])).status_code in (403, 404)
    client.cookies.clear()
    assert (await demo_pay(client, {}, order["id"])).status_code == 401
    await client.post(f"{API}/cart/items", json={"product_id": product["id"], "quantity": 1})                              # a guest buys
    guest_order = (await client.post(f"{API}/orders/checkout", json={"guest_email": "g@example.com", "shipping_address": ADDR}, headers={"Idempotency-Key": uuid.uuid4().hex})).json()
    assert (await client.post(f"{API}/payments", json={"order_id": guest_order["id"]})).status_code == 201
    paid = await demo_pay(client, {}, guest_order["id"])
    assert paid.status_code == 200 and paid.json()["order_status"] == "paid"
    assert (await client.get(f"{API}/orders/guest/{guest_order['id']}")).json()["demo_payment"] is True
    other = await demo_pay(client, {}, order["id"])                                                                        # the guest's cookie is for ITS order only
    assert other.status_code in (401, 404)


async def test_paying_with_demo_details_an_order_started_with_another_gateway_is_refused(client, admin_headers, category, world):
    live = await client.post(f"{API}/admin/payment-providers", json={"provider": "stripe", "mode": "test", "credentials": {"secret_key": "sk_test_abc12345"}, "currencies": ["usd"]}, headers=admin_headers)
    assert live.status_code == 201
    cid = live.json()["id"]
    assert (await client.post(f"{API}/admin/payment-providers/{cid}/validate", headers=admin_headers)).json()["valid"] is True
    assert (await client.post(f"{API}/admin/payment-providers/{cid}/enable", headers=admin_headers)).status_code == 200
    assert (await client.post(f"{API}/admin/payment-providers/{cid}/prefer", headers=admin_headers)).status_code == 200
    headers, order, _ = await pending_order(client, admin_headers, category)
    assert (await start_payment(client, headers, order["id"]))["provider"] == "stripe"
    refused = await demo_pay(client, headers, order["id"])
    assert refused.status_code == 409 and refused.json()["code"] == "not_a_demo_payment"                                   # demo can never complete a real gateway's payment


# ============================================================================================== webhook security
async def forge(db_session, order_id, *, amount=None, event_id=None, age=0, secret=None):
    """A correctly signed demo event for this order's payment, built the way an attacker with the stored secret could (a test of the checks)."""
    row = (await db_session.execute(text("SELECT p.provider_payment_id, p.amount_cents, p.currency, c.webhook_secret_ciphertext FROM payments p "
                                         "JOIN payment_provider_configs c ON c.id = p.provider_config_id WHERE p.order_id = :o"), {"o": order_id})).one()
    key = secret or get_cipher().decrypt(row[3])
    payload = build_event(event_id=event_id or f"evt_demo_{uuid.uuid4().hex}", event_type="payment_intent.succeeded", provider_payment_id=row[0],
                          amount_cents=amount if amount is not None else row[1], currency=row[2])
    return payload, sign(key, payload, timestamp=int(time.time()) - age)


async def post_event(client, payload, signature):
    return await client.post(f"{API}/payments/webhooks/demo", content=payload, headers={"x-demo-signature": signature, "content-type": "application/json"})


async def test_the_demo_webhook_accepts_only_correctly_signed_current_events_for_the_right_amount(client, admin_headers, category, world, db_session):
    await enable_demo(client, admin_headers)
    headers, order, _ = await pending_order(client, admin_headers, category, price=7000)
    await start_payment(client, headers, order["id"])
    payload, good = await forge(db_session, order["id"])
    assert (await post_event(client, payload, "")).status_code in (400, 401, 422)
    assert (await post_event(client, payload, "t=1,v1=00")).status_code == 400
    assert (await post_event(client, payload.replace(b"7000", b"1"), good)).status_code == 400                             # tampered
    assert (await post_event(client, *(await forge(db_session, order["id"], age=3600)))).status_code == 400               # a replay of an old signature
    assert (await post_event(client, *(await forge(db_session, order["id"], secret="not-the-secret")))).status_code == 400
    assert (await client.get(f"{API}/orders/{order['id']}", headers=headers)).json()["status"] == "pending"                # nothing got through
    short = await post_event(client, *(await forge(db_session, order["id"], amount=1)))                                    # genuinely signed, wrong amount
    assert short.status_code == 200
    assert (await client.get(f"{API}/orders/{order['id']}", headers=headers)).json()["status"] == "pending"                # never marked paid
    flagged = (await client.get(f"{API}/admin/reconciliations", headers=admin_headers)).json()
    assert flagged["total"] >= 1
    event_id = f"evt_demo_{uuid.uuid4().hex}"
    first = await post_event(client, *(await forge(db_session, order["id"], event_id=event_id)))
    second = await post_event(client, *(await forge(db_session, order["id"], event_id=event_id)))                         # a duplicate delivery
    assert first.status_code == second.status_code == 200
    assert (await client.get(f"{API}/orders/{order['id']}", headers=headers)).json()["status"] == "paid"
    await db_session.rollback()
    assert (await db_session.execute(text("SELECT count(*) FROM order_status_history WHERE lower(to_status) = 'paid'"))).scalar_one() == 1


# ============================================================================================== demo money is never a sale
async def test_demo_orders_are_left_out_of_reports_and_invoices_and_can_be_refunded(client, admin_headers, category, world, test_session_factory, db_session):
    await enable_demo(client, admin_headers)
    headers, order, _ = await pending_order(client, admin_headers, category, price=9000)
    await start_payment(client, headers, order["id"])
    assert (await demo_pay(client, headers, order["id"])).json()["succeeded"] is True
    summary = (await client.get(f"{API}/admin/reports/summary", headers=admin_headers)).json()
    assert (summary["orders"], summary["gross_cents"], summary["demo_orders_excluded"]) == (0, 0, 1)                      # not a sale
    assert (await client.get(f"{API}/admin/reports/sales-by-day", headers=admin_headers)).json()["demo_orders_excluded"] == 1
    assert (await fulfil(client, admin_headers, order["id"])).status_code == 200
    invoice = await client.post(f"{API}/orders/{order['id']}/invoice", headers=admin_headers)
    assert invoice.status_code == 422 and "demo" in invoice.json()["detail"].lower()
    assert (await client.post(f"{API}/payments/{order['id']}/refund", headers=admin_headers)).status_code == 202            # the refund flow works too
    await jobs(test_session_factory)
    assert (await client.get(f"{API}/orders/{order['id']}", headers=headers)).json()["status"] == "refunded"
    await db_session.rollback()
    assert (await db_session.execute(text("SELECT count(*) FROM refunds WHERE status = 'succeeded'"))).scalar_one() == 1                   # a refund really ran, through the demo gateway
    assert (await db_session.execute(text("SELECT count(*) FROM invoices"))).scalar_one() == 0


# ============================================================================================== test and live gateways
async def add_stripe(client, admin_headers, mode, secret, webhook=None):
    body = {"provider": "stripe", "mode": mode, "credentials": {"secret_key": secret}, "currencies": ["usd"], **({"webhook_secret": webhook} if webhook else {})}
    r = await client.post(f"{API}/admin/payment-providers", json=body, headers=admin_headers)
    assert r.status_code == 201, r.text
    return r.json()


async def test_a_test_gateway_is_checked_from_the_admin_panel_without_exposing_its_secret(client, admin_headers, world):
    config = await add_stripe(client, admin_headers, "test", "sk_test_DISTINCTIVE_TEST_SECRET_9999", "whsec_DISTINCTIVE_TEST_WEBHOOK_9999")
    cid = config["id"]
    assert (await client.post(f"{API}/admin/payment-providers/{cid}/validate", headers=admin_headers)).json()["valid"] is True
    result = (await client.post(f"{API}/admin/payment-providers/{cid}/check", headers=admin_headers)).json()
    by_name = {c["check"]: c for c in result["checks"]}
    assert result["ok"] is True and by_name["credentials"]["status"] == "pass" and by_name["test_payment"]["status"] == "pass"
    assert by_name["currencies"]["status"] == "pass" and by_name["webhook_secret"]["status"] == "pass"
    assert by_name["webhook_delivery"]["status"] == "warn" and "/api/v1/payments/webhooks/stripe" in by_name["webhook_delivery"]["message"]
    assert world.created and world.created[0].startswith("gateway-check:")                                                  # a sandbox test payment really was made
    ov = await overview(client, admin_headers)
    gateway = next(g for g in ov["gateways"] if g["id"] == cid)
    assert gateway["last_check_ok"] is True and gateway["connection"] == "credentials_ok"                                 # not "connected": no webhook seen yet
    world.fail_create = True
    failed = (await client.post(f"{API}/admin/payment-providers/{cid}/check", headers=admin_headers)).json()
    assert failed["ok"] is False and {c["check"]: c for c in failed["checks"]}["test_payment"]["status"] == "fail"
    everything = json.dumps([await overview(client, admin_headers), (await client.get(f"{API}/admin/payment-providers", headers=admin_headers)).json(), result, failed])
    assert "DISTINCTIVE_TEST_SECRET" not in everything and "DISTINCTIVE_TEST_WEBHOOK" not in everything and "sk_test_" not in everything   # write-only secrets
    test_mode = await overview(client, admin_headers)
    assert test_mode["payment_mode"] == "none"                                                                              # configured, but not selected yet
    for step in ("enable", "prefer"):
        assert (await client.post(f"{API}/admin/payment-providers/{cid}/{step}", headers=admin_headers)).status_code == 200
    assert (await overview(client, admin_headers))["payment_mode"] == "test"
    assert (await client.get(f"{API}/store/settings")).json()["payment_mode"] == "test"


async def test_the_demo_gateway_check_passes_end_to_end(client, admin_headers, world):
    ov = await enable_demo(client, admin_headers)
    cid = ov["preferred"]["id"]
    result = (await client.post(f"{API}/admin/payment-providers/{cid}/check", headers=admin_headers)).json()
    assert result["ok"] is True and {c["check"]: c["status"] for c in result["checks"]}["test_payment"] == "pass"
    assert (await overview(client, admin_headers))["preferred"]["last_check_ok"] is True


async def make_live_ready(client, admin_headers, db_session, *, check=True):
    config = await add_stripe(client, admin_headers, "live", LIVE_SECRET, LIVE_WEBHOOK)
    cid = config["id"]
    assert (await client.post(f"{API}/admin/payment-providers/{cid}/validate", headers=admin_headers)).json()["valid"] is True
    early = await client.post(f"{API}/admin/payment-providers/{cid}/enable", headers=admin_headers)
    assert early.status_code == 409 and "webhook_not_verified" in early.json()["detail"]                                    # live needs a delivered signed event
    await db_session.execute(text("UPDATE payment_provider_configs SET webhook_status = 'verified', webhook_verified_at = now() WHERE id = :c"), {"c": cid})
    await db_session.commit()                                                                                               # (what the first genuine signed event does)
    for kind in ("sandbox_verified", "production_review"):
        assert (await client.post(f"{API}/admin/payment-providers/{cid}/attestations", json={"kind": kind, "reference": "ticket-123 done"}, headers=admin_headers)).status_code == 200
    assert (await client.post(f"{API}/admin/payment-providers/{cid}/enable", headers=admin_headers)).status_code == 200
    if check:
        assert (await client.post(f"{API}/admin/payment-providers/{cid}/check", headers=admin_headers)).json()["ok"] is True
    return cid


async def activate(client, admin_headers, cid, confirmation="ACTIVATE LIVE PAYMENTS"):
    return await client.post(f"{API}/admin/payments/live/activate", json={"config_id": cid, "confirmation": confirmation, "note": "Client approved"}, headers=admin_headers)


async def test_live_payments_stay_off_until_every_step_is_done_and_an_administrator_activates_them(client, admin_headers, world, db_session):
    assert (await overview(client, admin_headers))["live"]["activation_blockers"] == ["no_live_gateway_configured"]
    cid = await make_live_ready(client, admin_headers, db_session, check=False)
    ov = await overview(client, admin_headers)
    assert (ov["payment_mode"], ov["live"]["active"], ov["live"]["can_activate"]) == ("none", False, False) and ov["live"]["activation_blockers"] == ["gateway_check_not_run"]
    blocked = await activate(client, admin_headers, cid)
    assert blocked.status_code == 409 and blocked.json()["code"] == "live_activation_blocked" and "gateway_check_not_run" in blocked.json()["detail"]
    ok = (await client.post(f"{API}/admin/payment-providers/{cid}/check", headers=admin_headers)).json()
    assert ok["ok"] is True and {c["check"]: c["status"] for c in ok["checks"]}["test_payment"] == "skipped"                # live checks are read-only
    assert not world.created                                                                                                  # nothing was created on the live gateway
    assert (await overview(client, admin_headers))["live"]["can_activate"] is True
    wrong = await activate(client, admin_headers, cid, "yes please")
    assert wrong.status_code == 409 and "confirmation_text_incorrect" in wrong.json()["detail"]
    customer, _ = await signup(client)
    assert (await client.post(f"{API}/admin/payments/live/activate", json={"config_id": cid, "confirmation": "ACTIVATE LIVE PAYMENTS"}, headers=customer)).status_code == 403
    live = (await activate(client, admin_headers, cid)).json()
    assert (live["payment_mode"], live["live"]["active"], live["payments_open"]) == ("live", True, True) and live["preferred"]["id"] == cid
    assert (await client.get(f"{API}/store/settings")).json()["payment_mode"] == "live"
    assert LIVE_SECRET not in json.dumps(live) and LIVE_WEBHOOK not in json.dumps(live)
    demo = await client.post(f"{API}/admin/payments/demo/enable", json={"confirm_no_real_money": True}, headers=admin_headers)
    assert demo.status_code == 409 and demo.json()["code"] == "live_payments_active"                                         # demo money can never mix with live


async def test_deactivating_live_payments_leaves_the_store_with_no_gateway_and_never_falls_back_to_demo(client, admin_headers, category, world, db_session):
    await enable_demo(client, admin_headers)
    cid = await make_live_ready(client, admin_headers, db_session)
    await activate(client, admin_headers, cid)
    short = await client.post(f"{API}/admin/payments/live/deactivate", json={"reason": "x"}, headers=admin_headers)
    assert short.status_code == 422
    off = (await client.post(f"{API}/admin/payments/live/deactivate", json={"reason": "Pausing for a pricing fix"}, headers=admin_headers)).json()
    assert (off["payment_mode"], off["payments_open"], off["live"]["active"], off["preferred"]) == ("none", False, False, None)
    headers, order, _ = await pending_order(client, admin_headers, category)
    refused = await client.post(f"{API}/payments", json={"order_id": order["id"]}, headers=headers)
    assert refused.status_code == 503 and refused.json()["code"] == "no_payment_provider_selected"                          # the demo gateway did NOT take over
    assert (await client.post(f"{API}/admin/payments/live/deactivate", json={"reason": "again"}, headers=admin_headers)).status_code == 409
    assert (await enable_demo(client, admin_headers))["payment_mode"] == "demo"                                             # an explicit choice, now allowed


async def test_the_deployment_kill_switch_a_stale_check_and_a_failed_check_each_block_activation(client, admin_headers, world, db_session, monkeypatch):
    cid = await make_live_ready(client, admin_headers, db_session)
    monkeypatch.setattr(get_settings(), "live_payments_enabled", False)
    off = await activate(client, admin_headers, cid)
    assert off.status_code == 409 and "live_payments_disabled_on_this_deployment" in off.json()["detail"]
    monkeypatch.setattr(get_settings(), "live_payments_enabled", True)
    await db_session.execute(text("UPDATE payment_provider_configs SET last_check_at = :t WHERE id = :c"), {"t": datetime.now(UTC) - timedelta(hours=30), "c": cid})
    await db_session.commit()
    stale = await activate(client, admin_headers, cid)
    assert stale.status_code == 409 and "gateway_check_out_of_date" in stale.json()["detail"]
    await db_session.execute(text("UPDATE payment_provider_configs SET last_check_at = now(), last_check_ok = false WHERE id = :c"), {"c": cid})
    await db_session.commit()
    failed = await activate(client, admin_headers, cid)
    assert failed.status_code == 409 and "gateway_check_failed" in failed.json()["detail"]
    test_cfg = await add_stripe(client, admin_headers, "test", "sk_test_abc12345")
    not_live = await activate(client, admin_headers, test_cfg["id"])
    assert not_live.status_code == 409 and "not_a_live_configuration" in not_live.json()["detail"]
    assert (await overview(client, admin_headers))["live"]["active"] is False


async def test_a_live_stripe_key_in_the_environment_is_always_refused_and_live_payments_cannot_start_without_activation(client, admin_headers, category, world, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "stripe_secret_key", "sk_live_from_the_environment")
    headers, order, _ = await pending_order(client, admin_headers, category)
    refused = await client.post(f"{API}/payments", json={"order_id": order["id"]}, headers=headers)
    assert refused.status_code == 503 and refused.json()["code"] == "live_payments_disabled"
    monkeypatch.setattr(get_settings(), "stripe_secret_key", "sk_test_back_to_test")
    cid = await make_live_ready(client, admin_headers, db_session)
    await db_session.execute(text("UPDATE payment_provider_configs SET is_preferred = true WHERE id = :c"), {"c": cid})      # forced, bypassing the admin step
    await db_session.commit()
    not_activated = await client.post(f"{API}/payments", json={"order_id": order["id"]}, headers=headers)
    assert not_activated.status_code == 503 and not_activated.json()["code"] == "provider_mode_not_allowed"
    assert (await overview(client, admin_headers))["payments_open"] is False


async def test_the_database_itself_refuses_a_live_demo_gateway_and_an_enabled_live_gateway_without_proof(client, admin_headers, world, db_session):
    await enable_demo(client, admin_headers)
    with pytest.raises(Exception, match="ck_provider_configs_demo_is_test"):
        await db_session.execute(text("UPDATE payment_provider_configs SET mode = 'live' WHERE provider = 'demo'"))
    await db_session.rollback()
    live = await add_stripe(client, admin_headers, "live", LIVE_SECRET, LIVE_WEBHOOK)
    with pytest.raises(Exception, match="ck_provider_configs"):
        await db_session.execute(text("UPDATE payment_provider_configs SET status = 'enabled', credentials_status = 'valid' WHERE id = :c"), {"c": live["id"]})
    await db_session.rollback()


async def test_concurrent_demo_payments_of_one_order_pay_it_once(client, admin_headers, category, world, db_session):
    await enable_demo(client, admin_headers)
    headers, order, _ = await pending_order(client, admin_headers, category)
    await start_payment(client, headers, order["id"])
    results = await asyncio.gather(*[demo_pay(client, headers, order["id"]) for _ in range(4)])
    assert all(r.status_code == 200 and r.json()["succeeded"] for r in results)
    await db_session.rollback()
    assert (await db_session.execute(text("SELECT count(*) FROM order_status_history WHERE lower(to_status) = 'paid'"))).scalar_one() == 1
    assert (await db_session.execute(text("SELECT count(*) FROM payments WHERE status = 'succeeded'"))).scalar_one() == 1


async def test_every_payment_mode_endpoint_requires_an_administrator(client, admin_headers):
    customer, _ = await signup(client)
    calls = [("get", "/admin/payments/overview", None), ("post", "/admin/payments/demo/enable", {"confirm_no_real_money": True}), ("post", "/admin/payments/demo/disable", None),
             ("post", "/admin/payments/live/activate", {"config_id": str(uuid.uuid4()), "confirmation": "ACTIVATE LIVE PAYMENTS"}),
             ("post", "/admin/payments/live/deactivate", {"reason": "testing it"}), ("post", f"/admin/payment-providers/{uuid.uuid4()}/check", None)]
    client.cookies.clear()
    for method, path, body in calls:
        kw = {"json": body} if body is not None else {}
        assert (await getattr(client, method)(f"{API}{path}", **kw)).status_code in (401, 403), path
        assert (await getattr(client, method)(f"{API}{path}", headers=customer, **kw)).status_code == 403, path
