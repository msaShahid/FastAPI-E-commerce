"""Phase 2.5: per-business GST configuration, readiness report. Fictional values only."""

import pathlib
import re

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.modules.audit.models.audit_log import AuditLog
from app.modules.auth.models.user import User
from app.shared.enums.roles import UserRole

API = "/api/v1"
T = f"{API}/admin/tax"


@pytest.fixture
async def admin2(client, db_session):
    await client.post(f"{API}/auth/register",
                      json={"username": "admin_two", "email": "admin2@example.com", "password": "AdminPassword123!"})
    tok = (await client.post(f"{API}/auth/login",
                             json={"email": "admin2@example.com", "password": "AdminPassword123!"})).json()["access_token"]
    await db_session.execute(User.__table__.update().where(User.email == "admin2@example.com").values(role=UserRole.ADMIN))
    await db_session.commit()
    return {"Authorization": f"Bearer {tok}"}


async def registration(client, h1, h2, approve=True, gstin="27ABCDE1234F1Z5", valid_from="2020-01-01", valid_to=None):
    r = (await client.post(f"{T}/registrations", headers=h1, json={
        "gstin": gstin, "legal_name": "Test Business", "state_code": gstin[:2],
        "valid_from": valid_from, "valid_to": valid_to})).json()
    if approve:
        assert (await client.post(f"{T}/registrations/{r['id']}/approve", headers=h2)).status_code == 200
    return r["id"]


def settings_body(rid, **over):
    body = {"pricing_mode": "exclusive", "place_of_supply_basis": "delivery_state",
            "shipping_tax_treatment": "not_taxed", "rounding_policy": "line_half_up",
            "registration_id": rid, "effective_from": "2020-01-01", "source_reference": "TEST-APPROVAL"}
    return {**body, **over}


async def make_settings(client, h1, h2, rid, approve=True, **over):
    s = await client.post(f"{T}/settings", headers=h1, json=settings_body(rid, **over))
    assert s.status_code == 201, s.text
    if approve:
        assert (await client.post(f"{T}/settings/{s.json()['id']}/approve", headers=h2)).status_code == 200
    return s.json()["id"]


# ------------------------------------------------------------------ authorization
@pytest.mark.parametrize(("method", "path"), [
    ("get", "/settings"), ("post", "/settings"), ("patch", "/settings/1"),
    ("post", "/settings/1/approve"), ("post", "/settings/1/retire"), ("get", "/readiness"),
])
async def test_only_administrators_can_use_settings_and_readiness(client, method, path):
    body = None if method == "get" else {}
    assert (await client.request(method.upper(), f"{T}{path}", json=body)).status_code in (401, 403)
    await client.post(f"{API}/auth/register",
                      json={"username": "plain_user", "email": "plain@example.com", "password": "UserPass123!"})
    tok = (await client.post(f"{API}/auth/login",
                             json={"email": "plain@example.com", "password": "UserPass123!"})).json()["access_token"]
    r = await client.request(method.upper(), f"{T}{path}", headers={"Authorization": f"Bearer {tok}"}, json=body)
    assert r.status_code == 403


# ------------------------------------------------------------------ validation: nothing is defaulted
@pytest.mark.parametrize("missing", ["pricing_mode", "place_of_supply_basis", "shipping_tax_treatment",
                                     "rounding_policy", "registration_id", "effective_from"])
async def test_every_choice_is_required_so_nothing_is_defaulted(client, admin_headers, admin2, missing):
    rid = await registration(client, admin_headers, admin2)
    body = settings_body(rid)
    body.pop(missing)
    assert (await client.post(f"{T}/settings", headers=admin_headers, json=body)).status_code == 422


@pytest.mark.parametrize("over", [
    {"pricing_mode": "maybe"}, {"place_of_supply_basis": "billing_state"}, {"shipping_tax_treatment": "zero"},
    {"rounding_policy": "banker"}, {"effective_to": "2019-01-01"}, {"registration_id": 0},
])
async def test_invalid_choices_are_rejected(client, admin_headers, admin2, over):
    rid = await registration(client, admin_headers, admin2)
    assert (await client.post(f"{T}/settings", headers=admin_headers, json=settings_body(rid, **over))).status_code == 422


async def test_registration_must_exist_and_not_be_retired(client, admin_headers, admin2):
    assert (await client.post(f"{T}/settings", headers=admin_headers, json=settings_body(99999))).status_code == 404
    rid = await registration(client, admin_headers, admin2)
    await client.post(f"{T}/registrations/{rid}/retire", headers=admin_headers)
    r = await client.post(f"{T}/settings", headers=admin_headers, json=settings_body(rid))
    assert r.status_code == 422 and r.json()["code"] == "tax_master_invalid_input"


# ------------------------------------------------------------------ approval workflow
async def test_settings_start_as_drafts_and_approval_needs_the_professionals_reference_and_an_approved_registration(
    client, admin_headers, admin2
):
    rid = await registration(client, admin_headers, admin2)
    draft_reg = await registration(client, admin_headers, admin2, approve=False, gstin="29ABCDE1234F1Z5")
    s = (await client.post(f"{T}/settings", headers=admin_headers, json=settings_body(rid, source_reference=None))).json()
    assert s["status"] == "draft" and s["approved_at"] is None
    r = await client.post(f"{T}/settings/{s['id']}/approve", headers=admin2)
    assert r.status_code == 422 and r.json()["code"] == "approval_prerequisite_missing"

    unapproved = (await client.post(f"{T}/settings", headers=admin_headers, json=settings_body(draft_reg))).json()
    r = await client.post(f"{T}/settings/{unapproved['id']}/approve", headers=admin2)
    assert r.status_code == 422 and "registration" in r.json()["detail"].lower()

    ok = (await client.post(f"{T}/settings", headers=admin_headers, json=settings_body(rid))).json()
    same = await client.post(f"{T}/settings/{ok['id']}/approve", headers=admin_headers)
    assert same.status_code == 403 and same.json()["code"] == "approval_requires_distinct_approver"
    done = await client.post(f"{T}/settings/{ok['id']}/approve", headers=admin2)
    assert done.status_code == 200 and done.json()["status"] == "approved" and done.json()["approved_at"]


async def test_approved_settings_cannot_be_edited_only_retired(client, admin_headers, admin2, db_session):
    rid = await registration(client, admin_headers, admin2)
    sid = await make_settings(client, admin_headers, admin2, rid)
    r = await client.patch(f"{T}/settings/{sid}", json={"pricing_mode": "inclusive"}, headers=admin_headers)
    assert r.status_code == 409 and r.json()["code"] == "tax_master_immutable"
    # the database refuses it as well, even if the service is bypassed
    for sql in ("UPDATE business_tax_settings SET pricing_mode = 'inclusive' WHERE id = :i",
                "UPDATE business_tax_settings SET rounding_policy = 'invoice_half_up' WHERE id = :i",
                "DELETE FROM business_tax_settings WHERE id = :i"):
        with pytest.raises(IntegrityError):
            await db_session.execute(text(sql), {"i": sid})
        await db_session.rollback()
    assert (await client.post(f"{T}/settings/{sid}/retire", headers=admin_headers)).status_code == 200


async def test_drafts_can_be_edited_but_required_fields_cannot_be_nulled(client, admin_headers, admin2):
    rid = await registration(client, admin_headers, admin2)
    sid = await make_settings(client, admin_headers, admin2, rid, approve=False, effective_to="2030-01-01")
    ok = await client.patch(f"{T}/settings/{sid}", json={"pricing_mode": "inclusive", "effective_to": None},
                            headers=admin_headers)
    assert ok.status_code == 200 and ok.json()["pricing_mode"] == "inclusive" and ok.json()["effective_to"] is None
    for field in ("pricing_mode", "rounding_policy", "effective_from", "registration_id"):
        r = await client.patch(f"{T}/settings/{sid}", json={field: None}, headers=admin_headers)
        assert r.status_code == 422 and r.json()["code"] == "tax_master_invalid_input", field


async def test_settings_changes_are_audited(client, admin_headers, admin2, db_session):
    rid = await registration(client, admin_headers, admin2)
    await make_settings(client, admin_headers, admin2, rid)
    actions = {a for (a,) in (await db_session.execute(select(AuditLog.action).where(AuditLog.action.like("tax.settings.%")))).all()}
    assert {"tax.settings.created", "tax.settings.approved"} <= actions


async def test_only_one_approved_configuration_applies_at_a_time(client, admin_headers, admin2, monkeypatch):
    rid = await registration(client, admin_headers, admin2)
    first = await make_settings(client, admin_headers, admin2, rid)
    second = (await client.post(f"{T}/settings", headers=admin_headers,
                                json=settings_body(rid, effective_from="2025-01-01"))).json()
    r = await client.post(f"{T}/settings/{second['id']}/approve", headers=admin2)
    assert r.status_code == 409 and r.json()["code"] == "settings_overlap"

    # the database guard reports the same stable code even if the application pre-check is bypassed
    from app.modules.tax.repositories.tax_repository import TaxRepository

    async def blind(self, *args, **kwargs):
        return None

    monkeypatch.setattr(TaxRepository, "overlapping_approved_settings", blind)
    r = await client.post(f"{T}/settings/{second['id']}/approve", headers=admin2)
    assert r.status_code == 409 and r.json()["code"] == "settings_overlap"
    monkeypatch.undo()

    # replace properly: retire the old one, then the new one can be approved
    assert (await client.post(f"{T}/settings/{first}/retire", headers=admin_headers)).status_code == 200
    assert (await client.post(f"{T}/settings/{second['id']}/approve", headers=admin2)).status_code == 200


async def test_a_registration_used_by_settings_cannot_be_retired(client, admin_headers, admin2):
    rid = await registration(client, admin_headers, admin2)
    sid = await make_settings(client, admin_headers, admin2, rid)
    r = await client.post(f"{T}/registrations/{rid}/retire", headers=admin_headers)
    assert r.status_code == 409 and r.json()["code"] == "registration_in_use"
    await client.post(f"{T}/settings/{sid}/retire", headers=admin_headers)
    assert (await client.post(f"{T}/registrations/{rid}/retire", headers=admin_headers)).status_code == 200


# ------------------------------------------------------------------ checkout is blocked by missing configuration
ADDR = {"recipient_name": "R", "line1": "1 Test St", "city": "C", "state": "MH", "postal_code": "400001", "country": "IN"}


async def _ready_product(client, h1, h2):
    """An active product with an approved classification and an approved rule effective today."""
    cat = (await client.post(f"{API}/categories", json={"name": "C", "description": None}, headers=h1)).json()["id"]
    cls = (await client.post(f"{T}/classifications", headers=h1, json={
        "kind": "hsn", "code": "1234", "description": "x", "source_reference": "R"})).json()["id"]
    await client.post(f"{T}/classifications/{cls}/approve", headers=h2)
    rule = (await client.post(f"{T}/rules", headers=h1, json={
        "classification_id": cls, "treatment": "taxable", "cgst_rate": "2.5", "sgst_utgst_rate": "2.5",
        "igst_rate": "5", "effective_from": "2020-01-01", "source_reference": "TEST-NOTIF"})).json()["id"]
    await client.post(f"{T}/rules/{rule}/approve", headers=h2)
    p = (await client.post(f"{API}/products", headers=h1, json={
        "name": "P", "price_cents": 1000, "sku": "P1", "stock": 10, "category_id": cat, "status": "active",
        "tax_classification_id": cls})).json()
    return p["id"]


async def _checkout(client, pid):
    await client.post(f"{API}/cart/items", json={"product_id": pid, "quantity": 1})
    return await client.post(f"{API}/orders/checkout", json={"guest_email": "g@example.com", "shipping_address": ADDR},
                             headers={"Idempotency-Key": "k-config"})


async def test_gst_checkout_is_blocked_without_an_approved_business_configuration(
    client, admin_headers, admin2, db_session, monkeypatch
):
    monkeypatch.setattr(get_settings(), "tax_calculation_enabled", True)
    pid = await _ready_product(client, admin_headers, admin2)  # product and rule are fine; configuration is missing
    r = await _checkout(client, pid)
    assert r.status_code == 422 and r.json()["code"] == "tax_calculation_blocked"
    assert "configuration (no_approved_tax_settings)" in r.json()["detail"]
    assert (await client.get(f"{API}/products/{pid}")).json()["stock"] == 10
    assert (await db_session.execute(text("SELECT count(*) FROM orders"))).scalar_one() == 0


async def test_gst_checkout_is_blocked_when_the_registration_is_not_valid_today(
    client, admin_headers, admin2, monkeypatch
):
    monkeypatch.setattr(get_settings(), "tax_calculation_enabled", True)
    pid = await _ready_product(client, admin_headers, admin2)
    rid = await registration(client, admin_headers, admin2, valid_to="2020-12-31")
    await make_settings(client, admin_headers, admin2, rid)
    r = await _checkout(client, pid)
    assert r.status_code == 422 and "registration_not_valid_on_date" in r.json()["detail"]


async def test_a_fully_configured_business_is_still_refused_because_no_calculator_exists(
    client, admin_headers, admin2, db_session, monkeypatch
):
    monkeypatch.setattr(get_settings(), "tax_calculation_enabled", True)
    pid = await _ready_product(client, admin_headers, admin2)
    rid = await registration(client, admin_headers, admin2)
    await make_settings(client, admin_headers, admin2, rid)
    r = await _checkout(client, pid)
    assert r.status_code == 503 and r.json()["code"] == "tax_engine_not_implemented"
    assert (await db_session.execute(text("SELECT count(*) FROM orders"))).scalar_one() == 0


# ------------------------------------------------------------------ readiness report
async def _blocker_codes(client, h):
    body = (await client.get(f"{T}/readiness", headers=h)).json()
    return body, {b["code"] for b in body["blockers"]}


async def test_a_fresh_business_is_not_ready_and_the_report_says_exactly_why(client, admin_headers):
    body, codes = await _blocker_codes(client, admin_headers)
    assert body["ready"] is False and body["settings_id"] is None
    assert codes == {"no_approved_tax_settings", "gst_calculator_not_implemented"}
    assert body["flags"] == {"tax_calculation_enabled": False, "tax_classification_required": False}
    assert body["payments"]["live_payments_enabled"] is False and body["payments"]["payment_currency"] == "usd"
    # INR is never assumed: the test store is configured in USD and USD is enabled, so no payment blocker.
    assert body["payments"]["blockers"] == []
    assert body["counts"]["active_products"] == 0


async def test_readiness_reports_registration_problems(client, admin_headers, admin2):
    rid = await registration(client, admin_headers, admin2, valid_to="2020-12-31")  # expired registration
    await make_settings(client, admin_headers, admin2, rid)
    body, codes = await _blocker_codes(client, admin_headers)
    assert "registration_not_valid_on_date" in codes and body["settings_id"] is None


async def test_readiness_counts_active_products_that_cannot_be_taxed(client, admin_headers, admin2):
    rid = await registration(client, admin_headers, admin2)
    sid = await make_settings(client, admin_headers, admin2, rid)
    cat = (await client.post(f"{API}/categories", json={"name": "C", "description": None}, headers=admin_headers)).json()["id"]
    cls = (await client.post(f"{T}/classifications", headers=admin_headers, json={
        "kind": "hsn", "code": "1234", "description": "x", "source_reference": "R"})).json()["id"]
    await client.post(f"{T}/classifications/{cls}/approve", headers=admin2)

    async def add(sku, cid=None):
        r = await client.post(f"{API}/products", headers=admin_headers, json={
            "name": sku, "price_cents": 100, "sku": sku, "category_id": cat, "status": "active", "tax_classification_id": cid})
        assert r.status_code == 201, r.text

    await add("NOCLASS")
    await add("NORULE", cls)
    body, codes = await _blocker_codes(client, admin_headers)
    assert body["settings_id"] == sid
    assert {"active_products_unclassified", "active_products_without_effective_rule",
            "gst_calculator_not_implemented"} <= codes
    assert body["counts"] == {"active_products": 2, "active_unclassified": 1,
                              "active_classification_not_approved": 0, "active_without_effective_rule": 1}

    await client.post(f"{T}/rules", headers=admin_headers, json={
        "classification_id": cls, "treatment": "taxable", "cgst_rate": "2.5", "sgst_utgst_rate": "2.5",
        "igst_rate": "5", "effective_from": "2020-01-01", "source_reference": "TEST-NOTIF"})
    rule = (await client.get(f"{T}/rules", headers=admin_headers)).json()["items"][0]["id"]
    await client.post(f"{T}/rules/{rule}/approve", headers=admin2)
    p = (await client.get(f"{API}/products", params={"search": "NOCLASS"}, headers=admin_headers)).json()["items"][0]
    await client.patch(f"{API}/products/{p['id']}", json={"tax_classification_id": cls}, headers=admin_headers)
    body, codes = await _blocker_codes(client, admin_headers)
    assert codes == {"gst_calculator_not_implemented"}  # everything else is satisfied


async def test_the_unimplemented_calculator_is_the_only_gate_left_when_everything_else_is_configured(
    client, admin_headers, admin2, monkeypatch
):
    rid = await registration(client, admin_headers, admin2)
    await make_settings(client, admin_headers, admin2, rid)
    body, codes = await _blocker_codes(client, admin_headers)
    assert codes == {"gst_calculator_not_implemented"} and body["ready"] is False
    monkeypatch.setattr("app.modules.tax.services.tax_service.GST_CALCULATOR_IMPLEMENTED", True)
    body, codes = await _blocker_codes(client, admin_headers)
    assert codes == set() and body["ready"] is True  # only then can the report turn green


async def test_the_readiness_report_changes_nothing(client, admin_headers, admin2, db_session):
    rid = await registration(client, admin_headers, admin2)
    await make_settings(client, admin_headers, admin2, rid)
    count = text("SELECT (SELECT count(*) FROM audit_logs), (SELECT count(*) FROM business_tax_settings), "
                 "(SELECT max(updated_at)::text FROM business_tax_settings)")
    before = (await db_session.execute(count)).one()
    for _ in range(3):
        assert (await client.get(f"{T}/readiness", headers=admin_headers)).status_code == 200
    await db_session.rollback()
    assert (await db_session.execute(count)).one() == before


# ------------------------------------------------------------------ no client facts in the platform
def test_no_gstin_or_tax_data_is_hardcoded_or_seeded_in_the_platform():
    gstin = re.compile(r"\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b")
    seeding = re.compile(r"INSERT\s+INTO\s+(tax_classifications|tax_rules|gst_registrations|business_tax_settings)", re.I)
    for root in ("app", "alembic", "scripts"):
        for path in pathlib.Path(root).rglob("*.py"):
            text_ = path.read_text(encoding="utf-8")
            assert not gstin.search(text_), f"GSTIN-like literal in {path}"
            assert not seeding.search(text_), f"tax data seeded in {path}"


def test_tax_activation_flags_default_to_off():
    s = get_settings()
    assert s.tax_calculation_enabled is False and s.live_payments_enabled is False
