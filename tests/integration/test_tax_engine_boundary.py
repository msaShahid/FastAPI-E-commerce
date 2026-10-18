"""Phase 2.4: the tax-engine boundary. NO GST is calculated; nothing here is a real rate or code."""

import json
import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.core.payments.interface import (
    PaymentIntentResult,
    PaymentProvider,
    RefundResult,
    WebhookEvent,
)
from app.main import app
from app.modules.audit.models.audit_log import AuditLog
from app.modules.auth.models.user import User
from app.modules.orders.dependencies.order_deps import get_tax_engine
from app.modules.orders.models.order import Order
from app.modules.payments.dependencies.payment_deps import get_payment_provider
from app.modules.tax.engine import (
    AMBIGUOUS_RULE,
    UNKNOWN_TREATMENT,
    FlatInterimTaxEngine,
    NotConfiguredTaxEngine,
    TaxEngineUnavailableError,
    TaxLine,
    TaxNotConfiguredError,
    TaxRequest,
    TaxResult,
    evaluate_readiness,
)
from app.shared.enums.roles import UserRole

API = "/api/v1"
T = f"{API}/admin/tax"
ADDR = {"recipient_name": "R", "line1": "1 Test St", "city": "C", "state": "MH", "postal_code": "400001", "country": "IN"}


# ------------------------------------------------------------------ fixtures / helpers
@pytest.fixture
async def admin2(client, db_session):
    await client.post(f"{API}/auth/register",
                      json={"username": "admin_two", "email": "admin2@example.com", "password": "AdminPassword123!"})
    tok = (await client.post(f"{API}/auth/login",
                             json={"email": "admin2@example.com", "password": "AdminPassword123!"})).json()["access_token"]
    await db_session.execute(User.__table__.update().where(User.email == "admin2@example.com").values(role=UserRole.ADMIN))
    await db_session.commit()
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
async def category(client, admin_headers):
    return (await client.post(f"{API}/categories", json={"name": "Eng Cat", "description": None},
                              headers=admin_headers)).json()["id"]


async def classification(client, h1, h2, code="1234"):
    c = (await client.post(f"{T}/classifications", headers=h1,
                           json={"kind": "hsn", "code": code, "description": "Test goods", "source_reference": "TEST-REF"})).json()
    assert (await client.post(f"{T}/classifications/{c['id']}/approve", headers=h2)).status_code == 200
    return c["id"]


async def approved_rule(client, h1, h2, cid, treatment="taxable", start="2020-01-01", end=None):
    rates = {"cgst_rate": "2.5", "sgst_utgst_rate": "2.5", "igst_rate": "5"} if treatment == "taxable" else {
        "cgst_rate": "0", "sgst_utgst_rate": "0", "igst_rate": "0"}
    r = (await client.post(f"{T}/rules", headers=h1, json={
        "classification_id": cid, "treatment": treatment, "effective_from": start, "effective_to": end,
        "source_reference": "TEST-NOTIF", **rates})).json()
    assert (await client.post(f"{T}/rules/{r['id']}/approve", headers=h2)).status_code == 200
    return r["id"]


async def approved_settings(client, h1, h2, *, reg_valid_to=None):
    """An approved business tax configuration under an approved (fictional) registration."""
    reg = (await client.post(f"{T}/registrations", headers=h1, json={
        "gstin": "27ABCDE1234F1Z5", "legal_name": "Test Business", "state_code": "27",
        "valid_from": "2020-01-01", "valid_to": reg_valid_to})).json()
    assert (await client.post(f"{T}/registrations/{reg['id']}/approve", headers=h2)).status_code == 200
    s = (await client.post(f"{T}/settings", headers=h1, json={
        "pricing_mode": "exclusive", "place_of_supply_basis": "delivery_state",
        "shipping_tax_treatment": "not_taxed", "rounding_policy": "line_half_up",
        "registration_id": reg["id"], "effective_from": "2020-01-01", "source_reference": "TEST-APPROVAL"})).json()
    assert (await client.post(f"{T}/settings/{s['id']}/approve", headers=h2)).status_code == 200
    return s["id"]


async def product(client, h, category, sku, cid=None, price=1000, stock=10):
    r = await client.post(f"{API}/products", headers=h, json={
        "name": f"Item {sku}", "description": None, "price_cents": price, "sku": sku, "stock": stock,
        "category_id": category, "status": "active", "tax_classification_id": cid})
    assert r.status_code == 201, r.text
    return r.json()


async def checkout(client, product_ids, key=None):
    for pid in product_ids:
        await client.post(f"{API}/cart/items", json={"product_id": pid, "quantity": 1})
    return await client.post(f"{API}/orders/checkout", json={"guest_email": "g@example.com", "shipping_address": ADDR},
                             headers={"Idempotency-Key": key or str(uuid.uuid4())})


async def order_count(db):
    return (await db.execute(select(func.count()).select_from(Order))).scalar_one()


async def stock(client, pid):
    return (await client.get(f"{API}/products/{pid}")).json()["stock"]


# ------------------------------------------------------------------ the contract (no database)
async def test_unconfigured_engine_always_raises_and_never_returns_zero():
    req = TaxRequest(lines=[], subtotal_paise=1000, shipping_paise=0, as_of=date(2030, 1, 1))
    with pytest.raises(TaxNotConfiguredError) as exc:
        await NotConfiguredTaxEngine().calculate(req)
    assert exc.value.code == "tax_not_configured"
    assert NotConfiguredTaxEngine.is_configured is False


@pytest.mark.parametrize("bad", [-1, True, "0", None, 1.5])
def test_a_tax_result_cannot_carry_an_invalid_amount(bad):
    with pytest.raises(TaxEngineUnavailableError):
        TaxResult(tax_paise=bad, snapshot={"engine": "x", "gst_calculated": False})


@pytest.mark.parametrize("snapshot", [{}, {"engine": "x"}, {"gst_calculated": False}])
def test_a_tax_result_must_carry_an_audit_snapshot(snapshot):
    with pytest.raises(TaxEngineUnavailableError):
        TaxResult(tax_paise=0, snapshot=snapshot)


@pytest.mark.parametrize(("subtotal", "rate", "tax"), [(1010, "5", 51), (1030, "5", 52), (1000, "18", 180), (1, "0.5", 0), (3, "50", 2)])
async def test_flat_interim_engine_rounds_half_up_with_decimal(subtotal, rate, tax):
    result = await FlatInterimTaxEngine(rate).calculate(
        TaxRequest(lines=[], subtotal_paise=subtotal, shipping_paise=0, as_of=date(2030, 1, 1)))
    assert result.tax_paise == tax
    assert result.snapshot["engine"] == "flat_interim" and result.snapshot["gst_calculated"] is False


class _Rule:
    def __init__(self, treatment):
        self.treatment = treatment


class _StubRepo:
    def __init__(self, statuses, rules):
        self._statuses, self._rules = statuses, rules

    async def classification_statuses(self, ids):
        return {i: s for i, s in self._statuses.items() if i in ids}

    async def approved_rules_covering(self, ids, as_of):
        return {i: r for i, r in self._rules.items() if i in ids}


async def test_readiness_reports_ambiguous_and_unknown_treatment_instead_of_guessing():
    repo = _StubRepo({1: "approved", 2: "approved"}, {1: [_Rule("taxable"), _Rule("exempt")], 2: [_Rule("weird")]})
    lines = [TaxLine(10, 1, 100, 1), TaxLine(11, 1, 100, 2)]
    blockers = await evaluate_readiness(repo, lines, date(2030, 1, 1))
    assert [(b.product_id, b.reason) for b in blockers] == [(10, AMBIGUOUS_RULE), (11, UNKNOWN_TREATMENT)]


# ------------------------------------------------------------------ interim engine through checkout
async def test_checkout_stores_an_explicit_tax_snapshot_that_reconciles(client, admin_headers, admin2, category, monkeypatch):
    monkeypatch.setattr(get_settings(), "tax_rate_percent", Decimal("18"))
    cid = await classification(client, admin_headers, admin2)
    a = await product(client, admin_headers, category, "A", cid=cid, price=1000)
    b = await product(client, admin_headers, category, "B", cid=None, price=500)
    r = await checkout(client, [a["id"], b["id"]])
    assert r.status_code == 201, r.text
    o = r.json()
    snap = o["tax_snapshot"]
    assert snap["engine"] == "flat_interim" and snap["gst_calculated"] is False and snap["classification_used"] is False
    assert snap["taxable_value_paise"] == o["subtotal_cents"] == 1500
    assert snap["tax_paise"] == o["tax_cents"] == 270 and snap["rate_percent"] == "18"
    assert snap["shipping_taxed"] is False
    assert {(l["product_id"], l["tax_classification_id"]) for l in snap["lines"]} == {(a["id"], cid), (b["id"], None)}  # noqa: E741
    assert o["total_cents"] == o["subtotal_cents"] + o["shipping_cents"] + o["tax_cents"]


async def test_changing_tax_settings_later_does_not_alter_a_stored_snapshot(client, admin_headers, category, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "tax_rate_percent", Decimal("10"))
    p = await product(client, admin_headers, category, "S")
    first = (await checkout(client, [p["id"]])).json()
    monkeypatch.setattr(s, "tax_rate_percent", Decimal("28"))
    again = (await client.get(f"{API}/orders/guest/{first['id']}")).json()
    assert again["tax_cents"] == first["tax_cents"] == 100 and again["tax_snapshot"]["rate_percent"] == "10"


async def test_unconfigured_tax_refuses_checkout_without_any_mutation(client, admin_headers, category, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "tax_rate_percent", None)
    p = await product(client, admin_headers, category, "U")
    r = await checkout(client, [p["id"]])
    assert r.status_code == 503 and r.json()["code"] == "checkout_not_configured"
    assert await order_count(db_session) == 0 and await stock(client, p["id"]) == 10


async def test_a_misbehaving_engine_can_never_become_a_confirmed_result(client, admin_headers, category, db_session):
    class Garbage:
        is_configured = True

        async def calculate(self, request):
            return 0  # "zero" instead of a TaxResult

    app.dependency_overrides[get_tax_engine] = lambda: Garbage()
    p = await product(client, admin_headers, category, "G")
    r = await checkout(client, [p["id"]])
    assert r.status_code == 503
    assert await order_count(db_session) == 0 and await stock(client, p["id"]) == 10


# ------------------------------------------------------------------ GST engine boundary (enabled)
@pytest.fixture
def gst_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "tax_calculation_enabled", True)


async def test_gst_enabled_blocks_an_unclassified_product_instead_of_assuming_zero(
    client, admin_headers, category, db_session, gst_on
):
    p = await product(client, admin_headers, category, "NC")
    r = await checkout(client, [p["id"]])
    assert r.status_code == 422 and r.json()["code"] == "tax_calculation_blocked"
    assert "missing_classification" in r.json()["detail"] and str(p["id"]) in r.json()["detail"]
    assert await order_count(db_session) == 0 and await stock(client, p["id"]) == 10


@pytest.mark.parametrize(("start", "end"), [("2999-01-01", None), ("2020-01-01", "2020-06-01")])
async def test_gst_enabled_blocks_when_no_rule_is_effective_today(
    client, admin_headers, admin2, category, db_session, gst_on, start, end
):
    cid = await classification(client, admin_headers, admin2)
    await approved_rule(client, admin_headers, admin2, cid, start=start, end=end)  # future / already expired
    p = await product(client, admin_headers, category, "NR", cid=cid)
    r = await checkout(client, [p["id"]])
    assert r.status_code == 422 and "no_applicable_rule" in r.json()["detail"]
    assert await order_count(db_session) == 0 and await stock(client, p["id"]) == 10


async def test_gst_enabled_with_no_rule_at_all_is_blocked(client, admin_headers, admin2, category, db_session, gst_on):
    cid = await classification(client, admin_headers, admin2)
    p = await product(client, admin_headers, category, "NRA", cid=cid)
    r = await checkout(client, [p["id"]])
    assert r.status_code == 422 and "no_applicable_rule" in r.json()["detail"]


@pytest.mark.parametrize("treatment", ["taxable", "exempt"])
async def test_a_ready_cart_is_still_refused_because_the_gst_engine_is_not_implemented(
    client, admin_headers, admin2, category, db_session, gst_on, treatment
):
    cid = await classification(client, admin_headers, admin2)
    await approved_rule(client, admin_headers, admin2, cid, treatment=treatment)
    await approved_settings(client, admin_headers, admin2)  # the business configuration is complete too
    p = await product(client, admin_headers, category, f"RDY-{treatment}", cid=cid)
    r = await checkout(client, [p["id"]])
    assert r.status_code == 503 and r.json()["code"] == "tax_engine_not_implemented"
    assert await order_count(db_session) == 0                # no zero-tax order, even for an "exempt" rule
    assert await stock(client, p["id"]) == 10                # no stock mutation
    assert len((await client.get(f"{API}/cart")).json()["items"]) == 1  # the cart is untouched


async def test_mixed_cart_names_only_the_products_that_block_tax(client, admin_headers, admin2, category, gst_on):
    cid = await classification(client, admin_headers, admin2)
    await approved_rule(client, admin_headers, admin2, cid)
    ok = await product(client, admin_headers, category, "MOK", cid=cid)
    bad = await product(client, admin_headers, category, "MBAD")
    r = await checkout(client, [ok["id"], bad["id"]])
    assert r.status_code == 422
    assert f"product {bad['id']} (missing_classification)" in r.json()["detail"]
    assert f"product {ok['id']} " not in r.json()["detail"]


async def test_gst_enabled_takes_precedence_over_a_configured_flat_rate(client, admin_headers, category, db_session, gst_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "tax_rate_percent", Decimal("18"))  # flat rate is set too
    p = await product(client, admin_headers, category, "PREC")
    r = await checkout(client, [p["id"]])
    assert r.status_code == 422 and r.json()["code"] == "tax_calculation_blocked"  # flat rate was NOT used
    assert await order_count(db_session) == 0


# ------------------------------------------------------------------ master data separation
async def test_product_and_checkout_requests_never_modify_the_tax_master(client, admin_headers, admin2, category, db_session):
    cid = await classification(client, admin_headers, admin2)
    rid = await approved_rule(client, admin_headers, admin2, cid)
    snapshot = text("""SELECT 'c', id::text, status, code, description, source_reference, updated_at::text FROM tax_classifications
                       UNION ALL SELECT 'r', id::text, status, treatment, igst_rate::text, source_reference, updated_at::text FROM tax_rules
                       ORDER BY 1, 2""")
    before = (await db_session.execute(snapshot)).all()
    p = await product(client, admin_headers, category, "SEP", cid=cid)
    await client.patch(f"{API}/products/{p['id']}", json={"price_cents": 2000, "unit": "nos"}, headers=admin_headers)
    assert (await checkout(client, [p["id"]])).status_code in (201, 409)
    await db_session.rollback()
    assert (await db_session.execute(snapshot)).all() == before
    assert rid  # the approved rule is still there, unchanged


# ------------------------------------------------------------------ payment safeguards
class FakeProvider(PaymentProvider):
    def __init__(self):
        self.refunds = []

    async def create_payment_intent(self, *, amount_cents, currency, metadata, idempotency_key):
        return PaymentIntentResult("pi_test_1", "pi_test_1_secret", "requires_payment_method")

    async def retrieve_payment_intent(self, *, provider_payment_id):
        return PaymentIntentResult(provider_payment_id, "secret", "requires_payment_method")

    def verify_and_parse_webhook(self, *, payload, signature_header):
        d = json.loads(payload)
        return WebhookEvent(d["event_id"], d["type"], d["pi"])

    async def create_refund(self, *, provider_payment_id, amount_cents, idempotency_key):
        self.refunds.append(provider_payment_id)
        return RefundResult("re_1", amount_cents, "succeeded")


async def _user_order(client, product_id):
    await client.post(f"{API}/auth/register",
                      json={"username": "payer_user", "email": "payer@example.com", "password": "UserPass123!"})
    tok = (await client.post(f"{API}/auth/login",
                             json={"email": "payer@example.com", "password": "UserPass123!"})).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    await client.post(f"{API}/cart/items", json={"product_id": product_id, "quantity": 1}, headers=h)
    o = await client.post(f"{API}/orders/checkout", json={"shipping_address": ADDR}, headers={**h, "Idempotency-Key": "pay-1"})
    assert o.status_code == 201, o.text
    return h, o.json()["id"]


async def test_a_live_stripe_key_is_refused_unless_live_payments_are_enabled(client, admin_headers, category, monkeypatch):
    provider = FakeProvider()
    app.dependency_overrides[get_payment_provider] = lambda: provider
    p = await product(client, admin_headers, category, "LIVE")
    h, order_id = await _user_order(client, p["id"])
    s = get_settings()
    monkeypatch.setattr(s, "stripe_secret_key", "sk_live_not_a_real_key")
    r = await client.post(f"{API}/payments", json={"order_id": order_id}, headers=h)
    assert r.status_code == 503 and r.json()["code"] == "live_payments_disabled"
    refund = await client.post(f"{API}/payments/{order_id}/refund", headers=admin_headers)
    assert refund.status_code == 503 and refund.json()["code"] == "live_payments_disabled"
    assert provider.refunds == []
    monkeypatch.setattr(s, "live_payments_enabled", True)  # an explicit decision lifts the guard
    assert (await client.post(f"{API}/payments", json={"order_id": order_id}, headers=h)).status_code == 201


async def test_test_mode_keys_are_unaffected_by_the_live_guard(client, admin_headers, category, monkeypatch):
    app.dependency_overrides[get_payment_provider] = lambda: FakeProvider()
    p = await product(client, admin_headers, category, "TEST")
    h, order_id = await _user_order(client, p["id"])
    monkeypatch.setattr(get_settings(), "stripe_secret_key", "sk_test_anything")
    assert (await client.post(f"{API}/payments", json={"order_id": order_id}, headers=h)).status_code == 201


async def test_a_late_payment_is_recorded_for_review_without_changing_the_order(
    client, admin_headers, category, db_session
):
    provider = FakeProvider()
    app.dependency_overrides[get_payment_provider] = lambda: provider
    p = await product(client, admin_headers, category, "LATE")
    h, order_id = await _user_order(client, p["id"])
    assert (await client.post(f"{API}/payments", json={"order_id": order_id}, headers=h)).status_code == 201
    await db_session.execute(text("UPDATE orders SET status = 'cancelled' WHERE id = :o"), {"o": order_id})
    await db_session.commit()

    body = json.dumps({"event_id": "evt_late", "type": "payment_intent.succeeded", "pi": "pi_test_1"})
    first = await client.post(f"{API}/payments/webhook", content=body, headers={"Stripe-Signature": "t=1,v1=x"})
    replay = await client.post(f"{API}/payments/webhook", content=body, headers={"Stripe-Signature": "t=1,v1=x"})
    assert first.status_code == replay.status_code == 200

    logs = (await db_session.execute(select(AuditLog).where(AuditLog.action == "payment.late_unreconciled"))).scalars().all()
    assert len(logs) == 1                                     # recorded once; the replay was deduplicated
    assert logs[0].details["order_id"] == order_id and logs[0].details["order_status"] == "cancelled"
    assert logs[0].actor_user_id is None
    status = (await db_session.execute(text("SELECT status FROM orders WHERE id = :o"), {"o": order_id})).scalar_one()
    assert status == "cancelled" and provider.refunds == []   # nothing automatic: no refund, no fulfilment


async def test_a_payment_event_for_an_unknown_payment_is_recorded_for_review(client, db_session):
    app.dependency_overrides[get_payment_provider] = lambda: FakeProvider()
    body = json.dumps({"event_id": "evt_unknown", "type": "payment_intent.succeeded", "pi": "pi_nobody"})
    r = await client.post(f"{API}/payments/webhook", content=body, headers={"Stripe-Signature": "t=1,v1=x"})
    assert r.status_code == 200
    log = (await db_session.execute(select(AuditLog).where(AuditLog.action == "payment.unmatched_success"))).scalar_one()
    assert log.details["provider_payment_id"] == "pi_nobody"
