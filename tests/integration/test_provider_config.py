"""
Configurable payment providers (Phase 2.10). Real PostgreSQL and HTTP.
  * Stripe configurations: real signature verification and parsing, FAKED outbound calls.
  * Razorpay: real adapter against MOCKED HTTP (httpx.MockTransport). Neither is sandbox-verified.
"""

import hashlib
import hmac
import json
import logging
import time
import uuid
from dataclasses import replace

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings
from app.core.payments.exceptions import PaymentProviderTimeoutError
from app.core.payments.interface import CredentialCheck, PaymentIntentResult, RefundResult
from app.core.payments.razorpay_provider import RazorpayPaymentProvider
from app.core.payments.registry import SPECS
from app.core.payments.stripe_provider import StripePaymentProvider
from app.core.secrets import get_cipher
from app.main import app

API = "/api/v1"
ADDR = {"recipient_name": "R", "line1": "1 Test St", "city": "C", "state": "MH", "postal_code": "400001", "country": "IN"}
CREDS = {
    ("stripe", "test"): {"secret_key": "sk_test_p210_AAAA1111"}, ("stripe", "live"): {"secret_key": "sk_live_p210_BBBB2222"},
    ("razorpay", "test"): {"key_id": "rzp_test_p210KEY", "key_secret": "rzp_secret_CCCC3333"},
    ("razorpay", "live"): {"key_id": "rzp_live_p210KEY", "key_secret": "rzp_secret_DDDD4444"},
}
WEBHOOK = {"stripe": "whsec_p210_stripe_secret", "razorpay": "rzp_whsec_p210_secret"}
ALL_SECRETS = [v for c in CREDS.values() for v in c.values() if "KEY" not in v] + list(WEBHOOK.values())


# ================================================================================================ the fake world
def mode_of(key: str) -> str:
    return "live" if key.startswith(("sk_live_", "rzp_live_")) else "test"


class World:
    """State shared by every adapter instance the resolver builds."""

    def __init__(self):
        self.stripe_created, self.stripe_refunds, self.stripe_cancels = [], [], []
        self.stripe_invalid, self.stripe_mode_override, self.stripe_fail = set(), {}, None
        self.rz_calls, self.rz_orders, self.rz_refunds, self.rz_fail = [], [], [], None

    def rz_handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.rz_calls.append((request.method, request.url.path, body))
        if self.rz_fail == "timeout":
            raise httpx.ConnectTimeout("slow")
        path = request.url.path
        if request.method == "GET" and path == "/v1/orders":
            return httpx.Response(401 if "BAD" in request.headers["authorization"] and False else 200, json={"items": []})
        if request.method == "POST" and path == "/v1/orders":
            self.rz_orders.append(body)
            return httpx.Response(200, json={"id": f"order_RZ{len(self.rz_orders)}", "status": "created", **body})
        if request.method == "GET" and path.startswith("/v1/orders/"):
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[1], "status": "created"})
        if request.method == "POST" and path.endswith("/refund"):
            self.rz_refunds.append((path, body))
            return httpx.Response(200, json={"id": f"rfnd_RZ{len(self.rz_refunds)}", "amount": body["amount"], "status": "processed", "currency": "INR"})
        return httpx.Response(404, json={})


class FakeStripe(StripePaymentProvider):
    """Real signature verification and parsing; every outbound call is faked and records WHICH key was used."""

    def __init__(self, world: World, secret_key: str, webhook_secret: str | None):
        super().__init__(secret_key=secret_key, webhook_secret=webhook_secret or "")
        self.w = world

    async def validate_credentials(self):
        key = self._secret_key
        if key in self.w.stripe_invalid:
            return CredentialCheck(False, "authentication_failed", mode_of(key))
        return CredentialCheck(True, None, self.w.stripe_mode_override.get(key, mode_of(key)))

    async def create_payment_intent(self, *, amount_cents, currency, metadata, idempotency_key):
        if self.w.stripe_fail == "timeout":
            raise PaymentProviderTimeoutError()
        self.w.stripe_created.append({"key": self._secret_key, "amount": amount_cents, "currency": currency})
        return PaymentIntentResult(f"pi_p210_{len(self.w.stripe_created)}", "secret", "requires_payment_method")

    async def retrieve_payment_intent(self, *, provider_payment_id):
        return PaymentIntentResult(provider_payment_id, "secret", "requires_payment_method")

    async def cancel_payment_intent(self, *, provider_payment_id, idempotency_key):
        self.w.stripe_cancels.append((self._secret_key, provider_payment_id))
        return PaymentIntentResult(provider_payment_id, "secret", "canceled")

    async def create_refund(self, *, provider_payment_id, amount_cents, currency, idempotency_key):
        self.w.stripe_refunds.append({"key": self._secret_key, "target": provider_payment_id, "amount": amount_cents})
        return RefundResult(f"re_p210_{len(self.w.stripe_refunds)}", amount_cents, "succeeded", currency)


@pytest.fixture
def world(monkeypatch):
    w = World()
    monkeypatch.setattr(get_settings(), "provider_secrets_key", SecretStr(Fernet.generate_key().decode()))
    monkeypatch.setitem(SPECS, "stripe", replace(SPECS["stripe"], factory=lambda c, wh: FakeStripe(w, c["secret_key"], wh)))
    monkeypatch.setitem(SPECS, "razorpay", replace(SPECS["razorpay"], factory=lambda c, wh: RazorpayPaymentProvider(
        key_id=c["key_id"], key_secret=c["key_secret"], webhook_secret=wh, transport=httpx.MockTransport(w.rz_handler))))
    return w


# ================================================================================================ helpers
@pytest.fixture
async def category(client, admin_headers):
    return (await client.post(f"{API}/categories", json={"name": "P210", "description": None}, headers=admin_headers)).json()["id"]


async def set_inr(client, admin_headers):
    assert (await client.put(f"{API}/store/currency", json={"currency": "inr"}, headers=admin_headers)).status_code == 200


async def add(client, admin_headers, provider="stripe", mode="test", **over):
    body = {"provider": provider, "mode": mode, "credentials": CREDS[(provider, mode)], "webhook_secret": WEBHOOK[provider],
            "currencies": ["inr"], **over}
    return await client.post(f"{API}/admin/payment-providers", json=body, headers=admin_headers)


async def activate(client, admin_headers, provider="stripe", mode="test", prefer=True):
    cfg = (await add(client, admin_headers, provider, mode)).json()
    base = f"{API}/admin/payment-providers/{cfg['id']}"
    assert (await client.post(f"{base}/validate", headers=admin_headers)).json()["valid"] is True
    r = await client.post(f"{base}/enable", headers=admin_headers)
    assert r.status_code == 200, r.text
    if prefer:
        assert (await client.post(f"{base}/prefer", headers=admin_headers)).status_code == 200
    return cfg["id"]


_n = {"i": 0, "e": 0}


async def make_order(client, admin_headers, category, qty=2):
    _n["i"] += 1
    i = _n["i"]
    p = (await client.post(f"{API}/products", headers=admin_headers, json={
        "name": f"P210 {i}", "description": None, "price_cents": 1000, "sku": f"P210-{i}", "stock": 10,
        "category_id": category, "status": "active"})).json()
    await client.post(f"{API}/auth/register", json={"username": f"p210_{i}", "email": f"p210{i}@example.com", "password": "UserPass123!"})
    tok = (await client.post(f"{API}/auth/login", json={"email": f"p210{i}@example.com", "password": "UserPass123!"})).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    await client.post(f"{API}/cart/items", json={"product_id": p["id"], "quantity": qty}, headers=h)
    r = await client.post(f"{API}/orders/checkout", json={"shipping_address": ADDR}, headers={**h, "Idempotency-Key": f"p210-{i}"})
    assert r.status_code == 201, r.text
    return h, r.json(), p


async def pay(client, h, order_id):
    return await client.post(f"{API}/payments", json={"order_id": order_id}, headers=h)


async def one(db, sql, **params):
    await db.rollback()
    return (await db.execute(text(sql), params)).scalar_one()


async def jobs(session_factory):
    import app.jobs  # noqa: F401
    from app.core.jobs.worker import run_one

    n = 0
    while await run_one(session_factory) and n < 30:
        n += 1


def stripe_post(client, kind, obj, secret=WEBHOOK["stripe"], event_id=None, path="stripe"):
    _n["e"] += 1
    body = json.dumps({"id": event_id or f"evt_p210_{_n['e']}", "object": "event", "type": kind, "data": {"object": obj}}).encode()
    ts = int(time.time())
    sig = f"t={ts},v1={hmac.new(secret.encode(), f'{ts}.'.encode() + body, hashlib.sha256).hexdigest()}"
    return client.post(f"{API}/payments/webhooks/{path}", content=body, headers={"Stripe-Signature": sig})


def rz_post(client, event, payload, secret=WEBHOOK["razorpay"], event_id=None, path="razorpay"):
    _n["e"] += 1
    body = json.dumps({"event": event, "payload": payload}).encode()
    headers = {"X-Razorpay-Signature": hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()}
    if event_id is not False:
        headers["X-Razorpay-Event-Id"] = event_id or f"rzev_{_n['e']}"
    return client.post(f"{API}/payments/webhooks/{path}", content=body, headers=headers)


def captured(order_id, pay_id="pay_P210_1", amount=2000):
    return {"payment": {"entity": {"id": pay_id, "order_id": order_id, "amount": amount, "currency": "INR", "status": "captured"}}}


def succeeded(pi, amount=2000, currency="inr"):
    return {"id": pi, "amount": amount, "amount_received": amount, "currency": currency}


# ================================================================================================ catalog, creation, redaction
async def test_the_catalog_states_what_each_provider_is_verified_to_support(client, admin_headers, world):
    catalog = {c["provider"]: c for c in (await client.get(f"{API}/admin/payment-providers/catalog", headers=admin_headers)).json()}
    assert set(catalog) == {"stripe", "razorpay"}
    assert catalog["razorpay"]["capabilities"]["currencies"] == ["inr"] and catalog["razorpay"]["capabilities"]["supports_cancel"] is False
    assert catalog["stripe"]["credential_fields"] == ["secret_key"] and catalog["razorpay"]["credential_fields"] == ["key_id", "key_secret"]
    assert "mocked" in catalog["razorpay"]["capabilities"]["verification"]


async def test_credentials_are_stored_encrypted_and_never_returned(client, admin_headers, world, db_session):
    r = await add(client, admin_headers, "razorpay")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "draft" and body["is_preferred"] is False and body["credentials"]["status"] == "unvalidated"
    assert body["credentials"]["hint"] == "...3333" and body["credentials"]["public_identifier"] == "rzp_test_p210KEY"
    listed = await client.get(f"{API}/admin/payment-providers", headers=admin_headers)
    detail = await client.get(f"{API}/admin/payment-providers/{body['id']}", headers=admin_headers)
    for text_ in (r.text, listed.text, detail.text):
        assert not any(secret in text_ for secret in ALL_SECRETS) and "ciphertext" not in text_
    row = (await db_session.execute(text("SELECT credentials_ciphertext, webhook_secret_ciphertext FROM payment_provider_configs"))).one()
    assert not any(secret in (row[0] + row[1]) for secret in ALL_SECRETS)                 # ciphertext only
    plain = json.loads(get_cipher().decrypt(row[0]))
    assert plain == CREDS[("razorpay", "test")] and get_cipher().decrypt(row[1]) == WEBHOOK["razorpay"]


async def test_without_an_encryption_key_nothing_is_stored_there_is_no_plaintext_fallback(client, admin_headers, monkeypatch, db_session):
    monkeypatch.setattr(get_settings(), "provider_secrets_key", None)
    r = await add(client, admin_headers)
    assert r.status_code == 503 and r.json()["code"] == "secret_storage_not_configured"
    assert await one(db_session, "SELECT count(*) FROM payment_provider_configs") == 0
    assert not any(secret in r.text for secret in ALL_SECRETS)


async def test_validation_errors_never_echo_what_was_submitted(client, admin_headers, world):
    leaked = "sk_test_p210_SHOULD_NOT_COME_BACK"
    for body in (
        {"provider": "stripe", "mode": "test", "credentials": {"secret_key": leaked, "key_id": 12345}},          # wrong shape
        {"provider": "stripe", "mode": "test", "credentials": {"secret_key": leaked}, "store_id": "other-store"},  # unknown field
        {"provider": "stripe", "mode": "sideways", "credentials": {"secret_key": leaked}},                       # bad enum
        {"provider": "stripe", "mode": "test", "credentials": {"secret_key": leaked, "oops": [leaked]}},
    ):
        r = await client.post(f"{API}/admin/payment-providers", json=body, headers=admin_headers)
        assert r.status_code == 422 and leaked not in r.text and "other-store" not in r.text, r.text


@pytest.mark.parametrize(("body", "status", "code"), [
    ({"provider": "paypal"}, 422, "provider_not_supported"),
    ({"provider": "stripe", "mode": "test", "credentials": {"secret_key": "sk_live_p210_BBBB2222"}}, 422, "credentials_mode_mismatch"),
    ({"provider": "stripe", "mode": "live", "credentials": {"secret_key": "sk_test_p210_AAAA1111"}}, 422, "credentials_mode_mismatch"),
    ({"provider": "razorpay", "mode": "test", "credentials": {"key_id": "rzp_live_x", "key_secret": "rzp_secret_CCCC3333"}}, 422, "credentials_mode_mismatch"),
    ({"provider": "razorpay", "mode": "test", "credentials": {"secret_key": "sk_test_p210_AAAA1111"}}, 422, "credentials_fields_invalid"),
    ({"provider": "stripe", "mode": "test", "credentials": {"secret_key": "sk_test_p210_AAAA1111", "key_id": "x"}}, 422, "credentials_fields_invalid"),
    ({"provider": "stripe", "mode": "test", "webhook_secret": "nope"}, 422, "webhook_secret_invalid"),
    ({"provider": "stripe", "mode": "test", "currencies": ["kwd"]}, 422, "currency_not_supported_by_provider"),
    ({"provider": "razorpay", "mode": "test", "currencies": ["usd"]}, 422, "currency_not_supported_by_provider"),
    ({"provider": "razorpay", "mode": "test", "currencies": ["zzz"]}, 422, "currency_unsupported"),
])
async def test_invalid_configurations_are_refused_with_actionable_codes(client, admin_headers, world, body, status, code):
    provider = body["provider"]
    full = {"mode": "test", "credentials": CREDS.get((provider, "test"), {"secret_key": "sk_test_x"}), "webhook_secret": WEBHOOK.get(provider, "whsec_x"),
            "currencies": ["inr"], **body}
    r = await client.post(f"{API}/admin/payment-providers", json=full, headers=admin_headers)
    assert r.status_code == status and r.json()["code"] == code, r.text


async def test_one_configuration_per_provider_and_mode(client, admin_headers, world):
    assert (await add(client, admin_headers)).status_code == 201
    dup = await add(client, admin_headers)
    assert dup.status_code == 409 and dup.json()["code"] == "provider_config_exists"
    assert (await add(client, admin_headers, mode="live")).status_code == 201           # a live one alongside the test one is fine


async def test_a_new_configuration_enables_and_selects_nothing(client, admin_headers, world, db_session):
    await add(client, admin_headers)
    assert await one(db_session, "SELECT count(*) FROM payment_provider_configs WHERE status <> 'draft' OR is_preferred") == 0


# ================================================================================================ authorization and isolation
PROVIDER_ROUTES = [("GET", "/catalog"), ("GET", ""), ("POST", ""), ("GET", f"/{uuid.uuid4()}"), ("PUT", f"/{uuid.uuid4()}/credentials"),
                   ("POST", f"/{uuid.uuid4()}/validate"), ("PUT", f"/{uuid.uuid4()}/currencies"), ("POST", f"/{uuid.uuid4()}/enable"),
                   ("POST", f"/{uuid.uuid4()}/disable"), ("POST", f"/{uuid.uuid4()}/prefer"), ("POST", f"/{uuid.uuid4()}/attestations"),
                   ("POST", f"/{uuid.uuid4()}/webhook-secret/retire-previous"), ("GET", f"/{uuid.uuid4()}/webhook"),
                   ("GET", f"/{uuid.uuid4()}/readiness")]


@pytest.mark.parametrize(("method", "suffix"), PROVIDER_ROUTES)
async def test_every_provider_configuration_route_refuses_anonymous_and_customer_callers(client, admin_headers, world, method, suffix):
    await client.post(f"{API}/auth/register", json={"username": "cust_p210", "email": "cust210@example.com", "password": "UserPass123!"})
    tok = (await client.post(f"{API}/auth/login", json={"email": "cust210@example.com", "password": "UserPass123!"})).json()["access_token"]
    body = {"json": {}} if method in ("POST", "PUT") else {}
    anon = await client.request(method, f"{API}/admin/payment-providers{suffix}", **body)
    cust = await client.request(method, f"{API}/admin/payment-providers{suffix}", headers={"Authorization": f"Bearer {tok}"}, **body)
    assert anon.status_code in (401, 403) and cust.status_code == 403, (method, suffix, anon.status_code, cust.status_code)


async def test_no_endpoint_takes_a_store_or_tenant_selector(client, admin_headers, world):
    paths = app.openapi()["paths"]
    params = {p["name"] for path, ops in paths.items() if "payment-providers" in path for op in ops.values() for p in op.get("parameters", [])}
    assert params <= {"config_id"}, params                                    # one deployment = one store: nothing selects another
    cfg = (await add(client, admin_headers)).json()
    for q in ("?store_id=other", "?tenant=other", "?organization_id=1"):
        r = await client.get(f"{API}/admin/payment-providers{q}", headers=admin_headers)
        assert r.status_code == 200 and [c["id"] for c in r.json()] == [cfg["id"]]   # ignored, never honoured


async def test_a_payment_request_cannot_choose_the_provider_or_the_store(client, admin_headers, category, world):
    await set_inr(client, admin_headers)
    stripe_id = await activate(client, admin_headers, "stripe")
    await activate(client, admin_headers, "razorpay", prefer=False)
    h, o, p = await make_order(client, admin_headers, category)
    r = await client.post(f"{API}/payments", json={"order_id": o["id"], "provider": "razorpay", "store_id": "x", "provider_config_id": str(uuid.uuid4())}, headers=h)
    assert r.status_code == 201 and r.json()["provider"] == "stripe"          # the store's preferred provider, whatever was asked
    assert world.rz_orders == [] and stripe_id


# ================================================================================================ state machine and constraints
async def test_enabling_needs_validated_credentials_and_currencies_and_preferring_needs_enabled(client, admin_headers, world):
    await set_inr(client, admin_headers)
    cfg = (await add(client, admin_headers)).json()
    base = f"{API}/admin/payment-providers/{cfg['id']}"
    refused = await client.post(f"{base}/enable", headers=admin_headers)
    assert refused.status_code == 409 and refused.json()["code"] == "provider_not_ready" and "credentials_not_validated" in refused.json()["detail"]
    assert (await client.post(f"{base}/prefer", headers=admin_headers)).status_code == 409
    assert (await client.post(f"{base}/validate", headers=admin_headers)).json()["valid"] is True
    await client.put(f"{base}/currencies", json={"currencies": []}, headers=admin_headers)
    assert "no_supported_currency_enabled" in (await client.post(f"{base}/enable", headers=admin_headers)).json()["detail"]
    await client.put(f"{base}/currencies", json={"currencies": ["inr"]}, headers=admin_headers)
    assert (await client.post(f"{base}/enable", headers=admin_headers)).json()["status"] == "enabled"
    ready = (await client.get(f"{base}/readiness", headers=admin_headers)).json()
    assert ready["ready_for_use"] is True and ready["ready_for_live"] is False and "not_a_live_configuration" in ready["live_blockers"]
    assert (await client.put(f"{base}/currencies", json={"currencies": []}, headers=admin_headers)).status_code == 409   # enabled needs one


async def test_failed_validation_marks_the_credentials_invalid_and_pauses_an_enabled_configuration(client, admin_headers, world):
    await set_inr(client, admin_headers)
    cid = await activate(client, admin_headers)
    world.stripe_invalid.add(CREDS[("stripe", "test")]["secret_key"])
    result = (await client.post(f"{API}/admin/payment-providers/{cid}/validate", headers=admin_headers)).json()
    assert result["valid"] is False and result["reason"] == "authentication_failed"
    assert result["config"]["status"] == "disabled" and result["config"]["is_preferred"] is False


async def test_credentials_for_the_wrong_mode_are_reported_invalid(client, admin_headers, world):
    cfg = (await add(client, admin_headers)).json()
    world.stripe_mode_override[CREDS[("stripe", "test")]["secret_key"]] = "live"        # the provider says this key is a live key
    result = (await client.post(f"{API}/admin/payment-providers/{cfg['id']}/validate", headers=admin_headers)).json()
    assert (result["valid"], result["reason"]) == (False, "mode_mismatch")


async def test_the_database_itself_refuses_contradictory_provider_states(client, admin_headers, world, db_session):
    a = (await add(client, admin_headers, "stripe")).json()["id"]
    b = (await add(client, admin_headers, "razorpay")).json()["id"]
    for sql in (
        f"UPDATE payment_provider_configs SET is_preferred = true WHERE id = '{a}'",                      # preferred but not enabled
        f"UPDATE payment_provider_configs SET status = 'enabled' WHERE id = '{a}'",                       # enabled without valid credentials
        f"UPDATE payment_provider_configs SET status = 'weird' WHERE id = '{a}'",
        f"UPDATE payment_provider_configs SET mode = 'live' WHERE id = '{b}'; UPDATE payment_provider_configs SET status='enabled', credentials_status='valid' WHERE id = '{b}'",  # live-enabled w/o webhook+attestations
        f"UPDATE payment_provider_configs SET provider = 'Bad Name' WHERE id = '{a}'",
        f"UPDATE payment_provider_configs SET provider = 'razorpay', mode = 'test' WHERE id = '{a}'",    # duplicate (provider, mode)
    ):
        with pytest.raises(DBAPIError):
            for statement in sql.split("; "):
                await db_session.execute(text(statement))
        await db_session.rollback()
    for cid in (a, b):                                                                                    # two valid + enabled + preferred
        await db_session.execute(text(f"UPDATE payment_provider_configs SET credentials_status='valid', status='enabled' WHERE id = '{cid}'"))
    await db_session.execute(text(f"UPDATE payment_provider_configs SET is_preferred = true WHERE id = '{a}'"))
    await db_session.commit()
    with pytest.raises(DBAPIError):
        await db_session.execute(text(f"UPDATE payment_provider_configs SET is_preferred = true WHERE id = '{b}'"))   # a SECOND preferred
    await db_session.rollback()


async def test_preferring_switches_atomically_and_respects_the_stores_currency(client, admin_headers, world):
    await set_inr(client, admin_headers)
    s, r = await activate(client, admin_headers, "stripe"), await activate(client, admin_headers, "razorpay")      # razorpay preferred last
    states = {c["provider"]: c["is_preferred"] for c in (await client.get(f"{API}/admin/payment-providers", headers=admin_headers)).json()}
    assert states == {"stripe": False, "razorpay": True}
    assert s and r


async def test_a_provider_that_does_not_support_the_stores_currency_cannot_be_preferred(client, admin_headers, world):
    # the test store defaults to USD: Razorpay is INR-only on this platform
    cfg = (await add(client, admin_headers, "razorpay")).json()
    base = f"{API}/admin/payment-providers/{cfg['id']}"
    await client.post(f"{base}/validate", headers=admin_headers)
    await client.post(f"{base}/enable", headers=admin_headers)
    refused = await client.post(f"{base}/prefer", headers=admin_headers)
    assert refused.status_code == 409 and refused.json()["code"] == "provider_currency_not_supported"


async def test_live_configurations_need_every_condition_and_entering_credentials_never_enables_live(client, admin_headers, world, monkeypatch):
    await set_inr(client, admin_headers)
    cfg = (await add(client, admin_headers, "stripe", "live")).json()
    base = f"{API}/admin/payment-providers/{cfg['id']}"
    assert cfg["status"] == "draft"
    await client.post(f"{base}/validate", headers=admin_headers)
    blocked = (await client.post(f"{base}/enable", headers=admin_headers)).json()["detail"]
    for reason in ("live_payments_disabled_on_this_deployment", "webhook_not_verified", "sandbox_verification_not_recorded", "production_review_not_recorded"):
        assert reason in blocked
    assert (await client.post(f"{base}/attestations", json={"kind": "production_review", "reference": "review-1"}, headers=admin_headers)).json()["code"] == "sandbox_verification_required"
    await client.post(f"{base}/attestations", json={"kind": "sandbox_verified", "reference": "stripe test-mode run 2026-10-19"}, headers=admin_headers)
    await client.post(f"{base}/attestations", json={"kind": "production_review", "reference": "go-live checklist signed off"}, headers=admin_headers)
    monkeypatch.setattr(get_settings(), "live_payments_enabled", True)
    still = (await client.post(f"{base}/enable", headers=admin_headers)).json()["detail"]
    assert "webhook_not_verified" in still and "sandbox" not in still               # the unverified webhook still blocks it
    assert (await stripe_post(client, "customer.created", {"id": "cus_1"}, secret=WEBHOOK["stripe"])).status_code == 200   # a correctly signed event arrives
    enabled = await client.post(f"{base}/enable", headers=admin_headers)
    assert enabled.status_code == 200 and enabled.json()["readiness"]["ready_for_live"] is True


async def test_rotation_pauses_new_payments_resets_evidence_and_keeps_the_old_webhook_secret_during_the_overlap(client, admin_headers, category, world, db_session):
    await set_inr(client, admin_headers)
    cid = await activate(client, admin_headers)
    base = f"{API}/admin/payment-providers/{cid}"
    h, o, p = await make_order(client, admin_headers, category)
    assert (await pay(client, h, o["id"])).status_code == 201
    old_pi = "pi_p210_1"
    rotated = await client.put(f"{base}/credentials", headers=admin_headers, json={
        "credentials": {"secret_key": "sk_test_p210_NEWKEY9999"}, "webhook_secret": "whsec_p210_rotated_secret"})
    body = rotated.json()
    assert rotated.status_code == 200 and body["version"] == 2 and body["status"] == "disabled" and body["is_preferred"] is False
    assert body["credentials"]["status"] == "unvalidated" and body["credentials"]["hint"] == "...9999" and body["webhook"]["previous_secret_active"] is True
    assert not any(s in rotated.text for s in ALL_SECRETS + ["sk_test_p210_NEWKEY9999", "whsec_p210_rotated_secret"])
    h2, o2, _ = await make_order(client, admin_headers, category)
    refused = await pay(client, h2, o2["id"])
    assert refused.status_code == 503 and refused.json()["code"] == "no_payment_provider_selected"        # new payments paused
    # an event signed with the OLD secret (already in flight) still verifies and is applied; the NEW secret works too
    assert (await stripe_post(client, "payment_intent.succeeded", succeeded(old_pi), secret=WEBHOOK["stripe"])).status_code == 200
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid"
    assert (await stripe_post(client, "customer.created", {"id": "cus"}, secret="whsec_p210_rotated_secret")).status_code == 200
    assert (await client.post(f"{base}/webhook-secret/retire-previous", headers=admin_headers)).json()["webhook"]["previous_secret_active"] is False
    assert (await stripe_post(client, "customer.created", {"id": "cus"}, secret=WEBHOOK["stripe"])).status_code == 400   # the old secret is now refused
    assert (await client.post(f"{base}/validate", headers=admin_headers)).json()["valid"] is True
    assert (await client.post(f"{base}/enable", headers=admin_headers)).status_code == 200


# ================================================================================================ provider selection: no silent failover
async def test_a_new_payment_uses_the_preferred_provider_and_records_which_configuration(client, admin_headers, category, world, db_session):
    await set_inr(client, admin_headers)
    cid = await activate(client, admin_headers, "razorpay")
    h, o, p = await make_order(client, admin_headers, category)
    r = await pay(client, h, o["id"])
    assert r.status_code == 201 and r.json()["provider"] == "razorpay" and r.json()["client_secret"] == "order_RZ1"
    assert world.rz_orders[0]["amount"] == 2000 and world.rz_orders[0]["currency"] == "INR"
    row = (await db_session.execute(text("SELECT provider, provider_config_id::text, amount_cents, currency FROM payments"))).one()
    assert tuple(row) == ("razorpay", cid, 2000, "inr")


@pytest.mark.parametrize("setup", ["none_selected", "disabled", "currency_unsupported", "live_not_allowed"])
async def test_an_ineligible_provider_refuses_the_payment_with_a_clear_code_and_never_switches(client, admin_headers, category, world, db_session, setup):
    await set_inr(client, admin_headers)
    other = await activate(client, admin_headers, "razorpay", prefer=False)            # an eligible alternative exists...
    # (an ENABLED configuration with invalid credentials cannot exist: a database constraint forbids it)
    expected = {"none_selected": "no_payment_provider_selected", "disabled": "no_payment_provider_selected",
                "currency_unsupported": "provider_currency_not_supported", "live_not_allowed": "provider_mode_not_allowed"}[setup]
    if setup != "none_selected":
        cid = await activate(client, admin_headers, "stripe")
        if setup == "disabled":
            await client.post(f"{API}/admin/payment-providers/{cid}/disable", headers=admin_headers)
        elif setup == "currency_unsupported":
            await db_session.execute(text(f"UPDATE payment_provider_configs SET currencies = '[\"eur\"]'::jsonb WHERE id = '{cid}'"))
            await db_session.commit()
        elif setup == "live_not_allowed":
            await db_session.execute(text("ALTER TABLE payment_provider_configs DISABLE TRIGGER USER"))
            await db_session.execute(text(f"UPDATE payment_provider_configs SET mode = 'live', webhook_status='verified', sandbox_verified_at=now(), production_review_at=now() WHERE id = '{cid}'"))
            await db_session.commit()
    h, o, p = await make_order(client, admin_headers, category)
    r = await pay(client, h, o["id"])
    assert r.status_code == 503 and r.json()["code"] == expected, r.text
    assert world.rz_orders == [] and world.stripe_created == []                           # the eligible alternative was NOT used
    assert await one(db_session, "SELECT count(*) FROM payments") == 0 and other


async def test_a_provider_outage_fails_the_payment_and_does_not_fall_back(client, admin_headers, category, world, db_session):
    await set_inr(client, admin_headers)
    await activate(client, admin_headers, "stripe")
    await activate(client, admin_headers, "razorpay", prefer=False)
    world.stripe_fail = "timeout"
    h, o, p = await make_order(client, admin_headers, category)
    r = await pay(client, h, o["id"])
    assert r.status_code in (502, 503, 504)
    assert world.rz_orders == [] and await one(db_session, "SELECT count(*) FROM payments") == 0
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "pending"
    world.stripe_fail = None
    assert (await pay(client, h, o["id"])).status_code == 201                              # a plain retry works


async def test_inr_is_the_default_currency_for_a_new_store_with_providers(client, admin_headers, category, world, db_session):
    await db_session.execute(text("DELETE FROM store_settings"))                           # a brand-new store
    await db_session.commit()
    await activate(client, admin_headers, "razorpay")                                      # prefer needs the store currency: INR by default
    h, o, p = await make_order(client, admin_headers, category)
    assert o["currency"] == "inr"
    assert (await pay(client, h, o["id"])).json()["provider"] == "razorpay"


# ================================================================================================ switching, disabling, reconciliation
async def test_switching_the_preferred_provider_leaves_existing_payments_and_records_untouched(client, admin_headers, category, world, db_session, test_session_factory):
    await set_inr(client, admin_headers)
    stripe_id = await activate(client, admin_headers, "stripe")
    h, o, p = await make_order(client, admin_headers, category)
    assert (await pay(client, h, o["id"])).json()["provider"] == "stripe"
    before = (await db_session.execute(text("SELECT id::text, provider, provider_config_id::text, amount_cents, currency, status::text FROM payments"))).all()
    order_before = await one(db_session, "SELECT row_to_json(o)::text FROM orders o WHERE id = :o", o=o["id"])
    razor_id = await activate(client, admin_headers, "razorpay")                            # the store now prefers Razorpay
    assert [tuple(r) for r in (await db_session.execute(text("SELECT id::text, provider, provider_config_id::text, amount_cents, currency, status::text FROM payments"))).all()] == [tuple(r) for r in before]
    assert await one(db_session, "SELECT row_to_json(o)::text FROM orders o WHERE id = :o", o=o["id"]) == order_before
    # retrying the SAME order continues the pending Stripe payment: no second payment, no Razorpay order
    again = await pay(client, h, o["id"])
    assert again.status_code == 201 and again.json()["provider"] == "stripe" and world.rz_orders == []
    assert await one(db_session, "SELECT count(*) FROM payments") == 1
    # its confirmation still arrives through Stripe's webhook and is applied
    assert (await stripe_post(client, "payment_intent.succeeded", succeeded("pi_p210_1"))).status_code == 200
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid"
    # a NEW order uses Razorpay
    h2, o2, _ = await make_order(client, admin_headers, category)
    assert (await pay(client, h2, o2["id"])).json()["provider"] == "razorpay" and stripe_id and razor_id


async def test_disabling_a_provider_blocks_new_payments_but_existing_ones_still_reconcile_and_refund(client, admin_headers, category, world, db_session, test_session_factory):
    await set_inr(client, admin_headers)
    cid = await activate(client, admin_headers, "stripe")
    h, o, p = await make_order(client, admin_headers, category)
    await pay(client, h, o["id"])
    disabled = await client.post(f"{API}/admin/payment-providers/{cid}/disable", headers=admin_headers)
    assert disabled.json()["status"] == "disabled" and disabled.json()["is_preferred"] is False
    h2, o2, _ = await make_order(client, admin_headers, category)
    assert (await pay(client, h2, o2["id"])).json()["code"] == "no_payment_provider_selected"           # new payments blocked
    # ...but the existing payment's webhook is verified and applied through the disabled configuration
    assert (await stripe_post(client, "payment_intent.succeeded", succeeded("pi_p210_1"))).status_code == 200
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid"
    # ...and its refund is sent through the SAME (disabled) configuration's credentials
    assert (await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)).status_code == 202
    await jobs(test_session_factory)
    assert world.stripe_refunds[0]["key"] == CREDS[("stripe", "test")]["secret_key"]
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "refunded"
    assert await one(db_session, "SELECT count(*) FROM payment_provider_configs") == 1                  # nothing was deleted


async def test_a_refund_uses_the_provider_that_created_the_payment_not_the_current_preference(client, admin_headers, category, world, db_session, test_session_factory):
    await set_inr(client, admin_headers)
    await activate(client, admin_headers, "stripe")
    h, o, p = await make_order(client, admin_headers, category)
    await pay(client, h, o["id"])
    await stripe_post(client, "payment_intent.succeeded", succeeded("pi_p210_1"))
    await activate(client, admin_headers, "razorpay")                                                    # preference moves to Razorpay
    await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)
    await jobs(test_session_factory)
    assert len(world.stripe_refunds) == 1 and world.rz_refunds == []
    assert world.stripe_refunds[0]["target"] == "pi_p210_1"


async def test_an_event_verified_by_one_configuration_cannot_apply_to_a_payment_of_another(client, admin_headers, category, world, db_session):
    await set_inr(client, admin_headers)
    await activate(client, admin_headers, "stripe")                                                      # TEST configuration
    h, o, p = await make_order(client, admin_headers, category)
    await pay(client, h, o["id"])
    live = (await add(client, admin_headers, "stripe", "live", webhook_secret="whsec_p210_live_secret")).json()    # a LIVE configuration, same provider
    r = await stripe_post(client, "payment_intent.succeeded", succeeded("pi_p210_1"), secret="whsec_p210_live_secret")
    assert r.status_code == 200
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "pending"   # NOT applied
    assert await one(db_session, "SELECT count(*) FROM payment_reconciliations WHERE kind = 'order_mismatch'") == 1
    assert live


# ================================================================================================ webhooks per provider
async def test_stripe_webhooks_verify_only_against_that_providers_stored_secret(client, admin_headers, world):
    await set_inr(client, admin_headers)
    cid = await activate(client, admin_headers, "stripe")
    assert (await stripe_post(client, "customer.created", {"id": "c1"})).status_code == 200
    assert (await stripe_post(client, "customer.created", {"id": "c2"}, secret="whsec_wrong_secret_value")).status_code == 400
    assert (await client.get(f"{API}/admin/payment-providers/{cid}", headers=admin_headers)).json()["webhook"]["status"] == "verified"
    assert (await stripe_post(client, "customer.created", {"id": "c3"}, path="paypal")).status_code == 404


async def test_the_url_alone_authenticates_nothing_and_providers_cannot_borrow_each_others_signatures(client, admin_headers, world):
    await set_inr(client, admin_headers)
    await activate(client, admin_headers, "stripe")
    await activate(client, admin_headers, "razorpay", prefer=False)
    body = json.dumps({"event": "payment.captured", "payload": captured("order_X")}).encode()
    unsigned = await client.post(f"{API}/payments/webhooks/razorpay", content=body)
    assert unsigned.status_code == 400
    stripe_secret_sig = {"X-Razorpay-Signature": hmac.new(WEBHOOK["stripe"].encode(), body, hashlib.sha256).hexdigest()}
    assert (await client.post(f"{API}/payments/webhooks/razorpay", content=body, headers=stripe_secret_sig)).status_code == 400   # Stripe's secret
    assert (await rz_post(client, "payment.captured", captured("order_X"), secret=WEBHOOK["razorpay"])).status_code == 200


async def test_a_provider_with_no_configuration_has_no_webhook_endpoint(client, admin_headers, world):
    await set_inr(client, admin_headers)
    await activate(client, admin_headers, "stripe")
    assert (await rz_post(client, "payment.captured", captured("order_X"))).status_code == 404


async def test_razorpay_payment_capture_is_applied_once_and_stores_the_payment_reference(client, admin_headers, category, world, db_session):
    await set_inr(client, admin_headers)
    await activate(client, admin_headers, "razorpay")
    h, o, p = await make_order(client, admin_headers, category)
    await pay(client, h, o["id"])
    for _ in range(3):                                                                                   # delivered three times, same event id
        assert (await rz_post(client, "payment.captured", captured("order_RZ1"), event_id="rzev_same")).status_code == 200
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid"
    assert await one(db_session, "SELECT provider_reference FROM payments") == "pay_P210_1"
    assert await one(db_session, "SELECT count(*) FROM order_status_history WHERE order_id = :o AND to_status::text = 'PAID'", o=o["id"]) == 1
    assert (await db_session.execute(text("SELECT provider, status, attempts FROM processed_webhook_events WHERE provider = 'razorpay'"))).one() == ("razorpay", "processed", 1)


async def test_razorpay_events_are_deduplicated_by_their_signed_body_whatever_the_event_id_header_says(client, admin_headers, category, world, db_session):
    await set_inr(client, admin_headers)
    await activate(client, admin_headers, "razorpay")
    h, o, p = await make_order(client, admin_headers, category)
    await pay(client, h, o["id"])
    body = json.dumps({"event": "payment.captured", "payload": captured("order_RZ1")}).encode()
    headers = {"X-Razorpay-Signature": hmac.new(WEBHOOK["razorpay"].encode(), body, hashlib.sha256).hexdigest()}
    for _ in range(2):
        assert (await client.post(f"{API}/payments/webhooks/razorpay", content=body, headers=headers)).status_code == 200
    assert await one(db_session, "SELECT count(*) FROM processed_webhook_events WHERE provider = 'razorpay'") == 1


async def test_razorpay_amount_mismatch_unmatched_unsupported_and_failed_events_are_safe(client, admin_headers, category, world, db_session):
    await set_inr(client, admin_headers)
    await activate(client, admin_headers, "razorpay")
    h, o, p = await make_order(client, admin_headers, category)
    await pay(client, h, o["id"])
    assert (await rz_post(client, "payment.captured", captured("order_RZ1", amount=1))).status_code == 200      # wrong amount
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "pending"
    assert await one(db_session, "SELECT count(*) FROM payment_reconciliations WHERE kind = 'amount_mismatch'") == 1
    assert (await rz_post(client, "payment.captured", captured("order_NOPE", "pay_Z"))).status_code == 200      # unmatched order reference
    assert await one(db_session, "SELECT count(*) FROM payment_reconciliations WHERE kind = 'unmatched_payment'") == 1
    assert (await rz_post(client, "settlement.processed", {})).status_code == 200                              # unsupported: kept, not dropped
    assert await one(db_session, "SELECT status FROM processed_webhook_events WHERE event_type = 'razorpay.settlement.processed'") == "ignored"
    assert (await rz_post(client, "payment.failed", captured("order_RZ1"))).status_code == 200
    assert await one(db_session, "SELECT status::text FROM payments") == "failed"
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "pending"


async def test_an_event_from_one_provider_can_never_touch_another_providers_payment(client, admin_headers, category, world, db_session):
    await set_inr(client, admin_headers)
    await activate(client, admin_headers, "stripe")
    await activate(client, admin_headers, "razorpay", prefer=False)
    h, o, p = await make_order(client, admin_headers, category)
    await pay(client, h, o["id"])                                                                      # a STRIPE payment: pi_p210_1
    assert (await rz_post(client, "payment.captured", captured("pi_p210_1"))).status_code == 200      # Razorpay names Stripe's id
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "pending"
    assert await one(db_session, "SELECT status::text FROM payments") == "pending"
    assert await one(db_session, "SELECT count(*) FROM payment_reconciliations WHERE kind = 'unmatched_payment'") == 1


async def test_a_razorpay_refund_goes_through_the_payment_reference_and_is_confirmed_by_the_provider(client, admin_headers, category, world, db_session, test_session_factory):
    await set_inr(client, admin_headers)
    await activate(client, admin_headers, "razorpay")
    h, o, p = await make_order(client, admin_headers, category)
    await pay(client, h, o["id"])
    await rz_post(client, "payment.captured", captured("order_RZ1"))
    assert (await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)).status_code == 202
    await jobs(test_session_factory)
    assert world.rz_refunds == [("/v1/payments/pay_P210_1/refund", world.rz_refunds[0][1])] and world.rz_refunds[0][1]["amount"] == 2000
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "refunded"
    assert await one(db_session, "SELECT status::text FROM payments") == "refunded"


async def test_a_razorpay_refund_confirmation_by_webhook_is_matched_through_the_payment_reference(client, admin_headers, category, world, db_session):
    await set_inr(client, admin_headers)
    await activate(client, admin_headers, "razorpay")
    h, o, p = await make_order(client, admin_headers, category)
    await pay(client, h, o["id"])
    await rz_post(client, "payment.captured", captured("order_RZ1"))
    refund_event = {"refund": {"entity": {"id": "rfnd_DASH", "payment_id": "pay_P210_1", "amount": 2000, "currency": "INR"}}}
    assert (await rz_post(client, "refund.processed", refund_event)).status_code == 200                # made in the Razorpay dashboard
    assert await one(db_session, "SELECT origin FROM refunds") == "provider"
    assert await one(db_session, "SELECT status::text FROM payments") == "refunded"
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "paid"   # an external refund never changes the order


async def test_expiring_a_razorpay_order_records_that_the_provider_cannot_cancel_and_a_late_capture_is_only_reconciled(client, admin_headers, category, world, db_session, test_session_factory):
    from app.core.jobs.schedule import enqueue_due

    await set_inr(client, admin_headers)
    await activate(client, admin_headers, "razorpay")
    h, o, p = await make_order(client, admin_headers, category)
    await pay(client, h, o["id"])
    await db_session.execute(text("UPDATE orders SET created_at = now() - interval '3 hours' WHERE id = :o"), {"o": o["id"]})
    await db_session.commit()
    await enqueue_due(test_session_factory, get_settings())
    await jobs(test_session_factory)
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "cancelled"
    outcomes = await one(db_session, "SELECT details->'provider_outcomes' FROM audit_logs WHERE action = 'order.cancelled'")
    assert outcomes[0]["outcome"] == "unsupported_cancel"                                              # recorded, never assumed cancelled
    assert (await rz_post(client, "payment.captured", captured("order_RZ1"))).status_code == 200      # the customer pays anyway, late
    assert await one(db_session, "SELECT status::text FROM orders WHERE id = :o", o=o["id"]) == "cancelled"
    assert await one(db_session, "SELECT count(*) FROM payment_reconciliations WHERE kind = 'late_payment'") == 1
    assert await one(db_session, "SELECT count(*) FROM refunds") == 0                                  # not auto-refunded


# ================================================================================================ secrets hygiene end to end
async def test_no_secret_appears_in_responses_audit_records_events_or_logs_across_the_whole_flow(client, admin_headers, category, world, db_session, test_session_factory, caplog):
    seen = []
    with caplog.at_level(logging.DEBUG):
        await set_inr(client, admin_headers)
        for provider in ("stripe", "razorpay"):
            r = await add(client, admin_headers, provider)
            seen.append(r.text)
            cid = r.json()["id"]
            for suffix, method in (("validate", "post"), ("enable", "post"), ("prefer", "post"), ("readiness", "get"), ("webhook", "get"), ("", "get")):
                seen.append((await getattr(client, method)(f"{API}/admin/payment-providers/{cid}" + (f"/{suffix}" if suffix else ""), headers=admin_headers)).text)
        seen.append((await client.put(f"{API}/admin/payment-providers/{cid}/credentials", headers=admin_headers, json={"webhook_secret": "rzp_whsec_p210_rotated"})).text)
        seen.append((await client.get(f"{API}/admin/payment-providers", headers=admin_headers)).text)
        h, o, p = await make_order(client, admin_headers, category)
        seen.append((await pay(client, h, o["id"])).text)
        seen.append((await stripe_post(client, "customer.created", {"id": "c"}, secret="whsec_wrong_value_here")).text)
    audit = (await db_session.execute(text("SELECT coalesce(string_agg(details::text, ' '), '') FROM audit_logs"))).scalar_one()
    rows = (await db_session.execute(text("SELECT coalesce(string_agg(row_to_json(c)::text, ' '), '') FROM (SELECT id, provider, mode, status, credential_hint, webhook_hint, public_identifier FROM payment_provider_configs) c"))).scalar_one()
    for secret in ALL_SECRETS + ["rzp_whsec_p210_rotated", "whsec_wrong_value_here"]:
        assert not any(secret in text_ for text_ in seen), secret
        assert secret not in audit and secret not in rows and secret not in caplog.text, secret
    assert await one(db_session, "SELECT count(*) FROM audit_logs WHERE action LIKE 'provider_config.%'") >= 8   # but the changes ARE audited
