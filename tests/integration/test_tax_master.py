"""Phase 2.2: GST master data. Fictional values only; nothing here is a real code, rate or GSTIN."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.modules.audit.models.audit_log import AuditLog
from app.modules.auth.models.user import User
from app.shared.enums.roles import UserRole

T = "/api/v1/admin/tax"


def make_gstin(state="27", pan="ABCDE1234F", entity="1", check="5"):
    """A fictional, format-valid GSTIN built from parts (never a real registration)."""
    return f"{state}{pan}{entity}Z{check}"


@pytest.fixture
async def admin2_headers(client, db_session):
    await client.post("/api/v1/auth/register",
                      json={"username": "admin_two", "email": "admin2@example.com", "password": "AdminPassword123!"})
    tok = (await client.post("/api/v1/auth/login",
                             json={"email": "admin2@example.com", "password": "AdminPassword123!"})).json()["access_token"]
    await db_session.execute(
        User.__table__.update().where(User.email == "admin2@example.com").values(role=UserRole.ADMIN)
    )
    await db_session.commit()
    return {"Authorization": f"Bearer {tok}"}


async def new_classification(client, h, code="1234", kind="hsn", ref="TEST-REF-1", **extra):
    r = await client.post(f"{T}/classifications",
                          json={"kind": kind, "code": code, "description": "Test goods", "source_reference": ref, **extra},
                          headers=h)
    assert r.status_code == 201, r.text
    return r.json()


async def approved_classification(client, h1, h2, code="1234"):
    c = await new_classification(client, h1, code=code)
    r = await client.post(f"{T}/classifications/{c['id']}/approve", headers=h2)
    assert r.status_code == 200, r.text
    return r.json()


def rule_body(cid, **over):
    body = {"classification_id": cid, "treatment": "taxable", "cgst_rate": "2.5", "sgst_utgst_rate": "2.5",
            "igst_rate": "5", "effective_from": "2030-01-01", "source_reference": "TEST-NOTIF-1"}
    return {**body, **over}


# ------------------------------------------------------------------ authorization
@pytest.mark.parametrize(("method", "path"), [
    ("get", "/classifications"), ("get", "/rules"), ("get", "/registrations"),
    ("post", "/classifications"), ("post", "/rules"), ("post", "/registrations"),
    ("post", "/classifications/1/approve"), ("post", "/rules/1/approve"), ("post", "/registrations/1/approve"),
    ("patch", "/rules/1"), ("post", "/rules/1/close"), ("post", "/rules/1/retire"),
])
async def test_only_administrators_can_use_the_tax_master(client, method, path):
    body = None if method == "get" else {}
    anon = await client.request(method.upper(), f"{T}{path}", json=body)
    assert anon.status_code in (401, 403)
    await client.post("/api/v1/auth/register",
                      json={"username": "plain_user", "email": "plain@example.com", "password": "UserPass123!"})
    tok = (await client.post("/api/v1/auth/login",
                             json={"email": "plain@example.com", "password": "UserPass123!"})).json()["access_token"]
    resp = await client.request(method.upper(), f"{T}{path}", headers={"Authorization": f"Bearer {tok}"}, json=body)
    assert resp.status_code == 403


# ------------------------------------------------------------------ classifications
@pytest.mark.parametrize(("kind", "code", "ok"), [
    ("hsn", "12", True), ("hsn", "1234", True), ("hsn", "123456", True), ("hsn", "12345678", True),
    ("hsn", "123", False), ("hsn", "123456789", False), ("hsn", "12a4", False),
    ("sac", "123456", True), ("sac", "1234", False),
])
async def test_classification_code_format(client, admin_headers, kind, code, ok):
    r = await client.post(f"{T}/classifications", headers=admin_headers,
                          json={"kind": kind, "code": code, "description": "x"})
    assert (r.status_code == 201) is ok, r.text


async def test_new_records_are_drafts_and_duplicates_are_refused(client, admin_headers):
    c = await new_classification(client, admin_headers)
    assert c["status"] == "draft" and c["approved_at"] is None and c["approved_by_user_id"] is None
    dup = await client.post(f"{T}/classifications", headers=admin_headers,
                            json={"kind": "hsn", "code": "1234", "description": "again"})
    assert dup.status_code == 409 and dup.json()["code"] == "classification_exists"


async def test_search_treats_wildcards_literally(client, admin_headers):
    await new_classification(client, admin_headers, code="1234")
    all_ = await client.get(f"{T}/classifications", params={"q": "Test"}, headers=admin_headers)
    pct = await client.get(f"{T}/classifications", params={"q": "%"}, headers=admin_headers)
    assert all_.json()["total"] == 1
    assert pct.json()["total"] == 0


async def test_approval_needs_a_source_reference_and_a_different_administrator(
    client, admin_headers, admin2_headers
):
    no_ref = await client.post(f"{T}/classifications", headers=admin_headers,
                               json={"kind": "hsn", "code": "5678", "description": "x"})
    r = await client.post(f"{T}/classifications/{no_ref.json()['id']}/approve", headers=admin2_headers)
    assert r.status_code == 422 and r.json()["code"] == "approval_prerequisite_missing"

    c = await new_classification(client, admin_headers)
    same = await client.post(f"{T}/classifications/{c['id']}/approve", headers=admin_headers)
    assert same.status_code == 403 and same.json()["code"] == "approval_requires_distinct_approver"

    ok = await client.post(f"{T}/classifications/{c['id']}/approve", headers=admin2_headers)
    assert ok.status_code == 200
    assert ok.json()["status"] == "approved" and ok.json()["approved_at"] and ok.json()["approved_by_user_id"]

    again = await client.post(f"{T}/classifications/{c['id']}/approve", headers=admin2_headers)
    assert again.status_code == 409 and again.json()["code"] == "tax_master_invalid_transition"


async def test_approved_records_cannot_be_edited(client, admin_headers, admin2_headers):
    c = await approved_classification(client, admin_headers, admin2_headers)
    r = await client.patch(f"{T}/classifications/{c['id']}", json={"description": "changed"}, headers=admin_headers)
    assert r.status_code == 409 and r.json()["code"] == "tax_master_immutable"
    draft = await new_classification(client, admin_headers, code="9999")
    ok = await client.patch(f"{T}/classifications/{draft['id']}", json={"description": "edited"}, headers=admin_headers)
    assert ok.status_code == 200 and ok.json()["description"] == "edited"


async def test_self_approval_is_off_by_default_and_needs_a_note_when_enabled(
    client, admin_headers, db_session, monkeypatch
):
    c = await new_classification(client, admin_headers)
    assert (await client.post(f"{T}/classifications/{c['id']}/approve", headers=admin_headers)).status_code == 403

    monkeypatch.setattr(get_settings(), "tax_master_allow_self_approval", True)
    no_note = await client.post(f"{T}/classifications/{c['id']}/approve", headers=admin_headers)
    assert no_note.status_code == 422 and no_note.json()["code"] == "self_approval_note_required"
    ok = await client.post(f"{T}/classifications/{c['id']}/approve", headers=admin_headers,
                           json={"note": "Approved in writing by the GST professional, ref TEST-1"})
    assert ok.status_code == 200
    log = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "tax.classification.approved"))).scalar_one()
    assert log.details["self_approved"] is True and "TEST-1" in log.details["note"]


# ------------------------------------------------------------------ rules
@pytest.mark.parametrize("over", [
    {"igst_rate": "0", "cgst_rate": "0", "sgst_utgst_rate": "0"},          # taxable with no rate
    {"igst_rate": "5", "cgst_rate": "2", "sgst_utgst_rate": "2"},          # components do not add up
    {"treatment": "exempt"},                                              # exempt must carry no rate
    {"effective_to": "2029-12-31"},                                       # ends before it starts
    {"igst_rate": "101", "cgst_rate": "50", "sgst_utgst_rate": "51"},      # out of range
])
async def test_invalid_rule_shapes_are_rejected(client, admin_headers, admin2_headers, over):
    c = await approved_classification(client, admin_headers, admin2_headers)
    r = await client.post(f"{T}/rules", json=rule_body(c["id"], **over), headers=admin_headers)
    assert r.status_code == 422, r.text


async def test_explicit_non_taxable_treatments_need_zero_rates(client, admin_headers, admin2_headers):
    c = await approved_classification(client, admin_headers, admin2_headers)
    body = rule_body(c["id"], treatment="nil_rated", cgst_rate="0", sgst_utgst_rate="0", igst_rate="0")
    assert (await client.post(f"{T}/rules", json=body, headers=admin_headers)).status_code == 201


async def test_rule_approval_prerequisites(client, admin_headers, admin2_headers):
    draft_class = await new_classification(client, admin_headers)
    r = await client.post(f"{T}/rules", json=rule_body(draft_class["id"]), headers=admin_headers)
    approve = await client.post(f"{T}/rules/{r.json()['id']}/approve", headers=admin2_headers)
    assert approve.status_code == 422 and "classification" in approve.json()["detail"].lower()

    c = await approved_classification(client, admin_headers, admin2_headers, code="5555")
    no_ref = await client.post(f"{T}/rules", json=rule_body(c["id"], source_reference=None), headers=admin_headers)
    approve = await client.post(f"{T}/rules/{no_ref.json()['id']}/approve", headers=admin2_headers)
    assert approve.status_code == 422 and approve.json()["code"] == "approval_prerequisite_missing"


async def test_overlapping_approved_rules_are_refused_and_succession_works(client, admin_headers, admin2_headers):
    c = await approved_classification(client, admin_headers, admin2_headers)
    first = (await client.post(f"{T}/rules", json=rule_body(c["id"]), headers=admin_headers)).json()
    assert (await client.post(f"{T}/rules/{first['id']}/approve", headers=admin2_headers)).status_code == 200

    clash = (await client.post(f"{T}/rules", json=rule_body(c["id"], effective_from="2031-01-01"),
                               headers=admin_headers)).json()
    r = await client.post(f"{T}/rules/{clash['id']}/approve", headers=admin2_headers)
    assert r.status_code == 409 and r.json()["code"] == "rule_overlap"

    # close the first rule (exclusive end), then the successor starting that day can be approved
    assert (await client.post(f"{T}/rules/{first['id']}/close", json={"effective_to": "2031-01-01"},
                              headers=admin_headers)).status_code == 200
    assert (await client.post(f"{T}/rules/{clash['id']}/approve", headers=admin2_headers)).status_code == 200

    # a rule's end date may be brought forward, never extended; drafts cannot be closed
    ext = await client.post(f"{T}/rules/{first['id']}/close", json={"effective_to": "2032-01-01"}, headers=admin_headers)
    assert ext.status_code == 422
    other = (await client.post(f"{T}/rules", json=rule_body(c["id"], effective_from="2040-01-01"),
                               headers=admin_headers)).json()
    assert (await client.post(f"{T}/rules/{other['id']}/close", json={"effective_to": "2041-01-01"},
                              headers=admin_headers)).status_code == 409


async def test_approved_rules_cannot_be_edited(client, admin_headers, admin2_headers):
    c = await approved_classification(client, admin_headers, admin2_headers)
    rule = (await client.post(f"{T}/rules", json=rule_body(c["id"]), headers=admin_headers)).json()
    await client.post(f"{T}/rules/{rule['id']}/approve", headers=admin2_headers)
    r = await client.patch(f"{T}/rules/{rule['id']}", json={"igst_rate": "12"}, headers=admin_headers)
    assert r.status_code == 409 and r.json()["code"] == "tax_master_immutable"


async def test_draft_rule_edit_is_revalidated(client, admin_headers, admin2_headers):
    c = await approved_classification(client, admin_headers, admin2_headers)
    rule = (await client.post(f"{T}/rules", json=rule_body(c["id"]), headers=admin_headers)).json()
    bad = await client.patch(f"{T}/rules/{rule['id']}", json={"igst_rate": "12"}, headers=admin_headers)
    assert bad.status_code == 422  # 2.5 + 2.5 != 12
    good = await client.patch(f"{T}/rules/{rule['id']}",
                              json={"igst_rate": "12", "cgst_rate": "6", "sgst_utgst_rate": "6"}, headers=admin_headers)
    assert good.status_code == 200 and Decimal(good.json()["igst_rate"]) == 12


async def test_a_classification_in_use_cannot_be_retired(client, admin_headers, admin2_headers):
    c = await approved_classification(client, admin_headers, admin2_headers)
    rule = (await client.post(f"{T}/rules", json=rule_body(c["id"]), headers=admin_headers)).json()
    blocked = await client.post(f"{T}/classifications/{c['id']}/retire", headers=admin_headers)
    assert blocked.status_code == 409 and blocked.json()["code"] == "classification_in_use"
    assert (await client.post(f"{T}/rules/{rule['id']}/retire", headers=admin_headers)).status_code == 200
    assert (await client.post(f"{T}/classifications/{c['id']}/retire", headers=admin_headers)).status_code == 200


async def test_database_blocks_overlapping_approved_rules_even_if_the_service_is_bypassed(
    client, admin_headers, admin2_headers, db_session
):
    c = await approved_classification(client, admin_headers, admin2_headers)
    insert = text("""INSERT INTO tax_rules (classification_id, treatment, cgst_rate, sgst_utgst_rate, igst_rate,
                  effective_from, effective_to, source_reference)
                  VALUES (:c, 'taxable', 2.5, 2.5, 5, :f, :t, 'TEST') RETURNING id""")
    approve = text("UPDATE tax_rules SET status = 'approved', approved_at = now() WHERE id = :i")
    first = (await db_session.execute(insert, {"c": c["id"], "f": date(2030, 1, 1), "t": date(2031, 1, 1)})).scalar_one()
    second = (await db_session.execute(insert, {"c": c["id"], "f": date(2030, 6, 1), "t": None})).scalar_one()
    await db_session.execute(approve, {"i": first})
    with pytest.raises(IntegrityError):
        await db_session.execute(approve, {"i": second})
    await db_session.rollback()


async def test_database_rejects_a_taxable_rule_with_no_rate(client, admin_headers, admin2_headers, db_session):
    c = await approved_classification(client, admin_headers, admin2_headers)
    with pytest.raises(IntegrityError):
        await db_session.execute(text(
            "INSERT INTO tax_rules (classification_id, treatment, cgst_rate, sgst_utgst_rate, igst_rate, "
            "effective_from) VALUES (:c, 'taxable', 0, 0, 0, :f)"), {"c": c["id"], "f": date(2030, 1, 1)})
    await db_session.rollback()


# ------------------------------------------------------------------ registrations
async def test_registration_validation_and_flow(client, admin_headers, admin2_headers, db_session):
    base = {"gstin": make_gstin(), "legal_name": "Test Business", "state_code": "27", "valid_from": "2030-01-01"}
    bad_prefix = await client.post(f"{T}/registrations", json={**base, "state_code": "29"}, headers=admin_headers)
    bad_format = await client.post(f"{T}/registrations", json={**base, "gstin": "NOTAGSTIN"}, headers=admin_headers)
    assert bad_prefix.status_code == 422 and bad_format.status_code == 422

    reg = await client.post(f"{T}/registrations", json={**base, "gstin": make_gstin().lower()}, headers=admin_headers)
    assert reg.status_code == 201 and reg.json()["gstin"] == make_gstin() and reg.json()["status"] == "draft"
    dup = await client.post(f"{T}/registrations", json=base, headers=admin_headers)
    assert dup.status_code == 409 and dup.json()["code"] == "registration_exists"

    rid = reg.json()["id"]
    assert (await client.post(f"{T}/registrations/{rid}/approve", headers=admin_headers)).status_code == 403
    assert (await client.post(f"{T}/registrations/{rid}/approve", headers=admin2_headers)).status_code == 200
    edit = await client.patch(f"{T}/registrations/{rid}", json={"legal_name": "Other"}, headers=admin_headers)
    assert edit.status_code == 409

    rows = (await db_session.execute(select(AuditLog).where(AuditLog.target_type == "gst_registration"))).scalars().all()
    assert {r.action for r in rows} >= {"tax.registration.created", "tax.registration.approved"}
    assert all(make_gstin() not in str(r.details) for r in rows)  # the GSTIN is never copied into the audit log


# ------------------------------------------------------------------ audit + listing
async def test_changes_are_audited_with_the_actor(client, admin_headers, admin2_headers, db_session):
    c = await approved_classification(client, admin_headers, admin2_headers)
    rule = (await client.post(f"{T}/rules", json=rule_body(c["id"]), headers=admin_headers)).json()
    await client.post(f"{T}/rules/{rule['id']}/approve", headers=admin2_headers)
    rows = (await db_session.execute(select(AuditLog).where(AuditLog.action.like("tax.%")))).scalars().all()
    actions = {r.action for r in rows}
    assert {"tax.classification.created", "tax.classification.approved", "tax.rule.created",
            "tax.rule.approved"} <= actions
    assert all(r.actor_user_id is not None for r in rows)


async def test_listing_filters_by_status_and_paginates(client, admin_headers, admin2_headers):
    await approved_classification(client, admin_headers, admin2_headers, code="1111")
    await new_classification(client, admin_headers, code="2222")
    drafts = await client.get(f"{T}/classifications", params={"status": "draft"}, headers=admin_headers)
    approved = await client.get(f"{T}/classifications", params={"status": "approved"}, headers=admin_headers)
    assert drafts.json()["total"] == 1 and approved.json()["total"] == 1
    page = await client.get(f"{T}/classifications", params={"page": 1, "page_size": 1}, headers=admin_headers)
    assert len(page.json()["items"]) == 1 and page.json()["total"] == 2


# ------------------------------------------------------------------ review fixes
@pytest.mark.parametrize("field", ["treatment", "igst_rate", "effective_from", "cgst_rate"])
async def test_explicit_null_on_a_required_rule_field_is_a_422_not_a_500(
    client, admin_headers, admin2_headers, field
):
    c = await approved_classification(client, admin_headers, admin2_headers)
    rule = (await client.post(f"{T}/rules", json=rule_body(c["id"]), headers=admin_headers)).json()
    r = await client.patch(f"{T}/rules/{rule['id']}", json={field: None}, headers=admin_headers)
    assert r.status_code == 422 and r.json()["code"] == "tax_master_invalid_input", r.text


async def test_explicit_null_on_required_classification_and_registration_fields(client, admin_headers):
    d = await new_classification(client, admin_headers, code="7777")
    r = await client.patch(f"{T}/classifications/{d['id']}", json={"description": None}, headers=admin_headers)
    assert r.status_code == 422 and r.json()["code"] == "tax_master_invalid_input"
    reg = (await client.post(f"{T}/registrations", headers=admin_headers, json={
        "gstin": make_gstin(), "legal_name": "Test Business", "state_code": "27", "valid_from": "2030-01-01"})).json()
    r = await client.patch(f"{T}/registrations/{reg['id']}", json={"legal_name": None}, headers=admin_headers)
    assert r.status_code == 422 and r.json()["code"] == "tax_master_invalid_input"


async def test_optional_fields_can_be_cleared_on_a_draft(client, admin_headers, admin2_headers):
    c = await approved_classification(client, admin_headers, admin2_headers)
    rule = (await client.post(f"{T}/rules", json=rule_body(c["id"], effective_to="2031-01-01"),
                              headers=admin_headers)).json()
    r = await client.patch(f"{T}/rules/{rule['id']}", json={"effective_to": None}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["effective_to"] is None


async def test_the_database_overlap_guard_reports_the_stable_error_even_if_the_precheck_misses(
    client, admin_headers, admin2_headers, monkeypatch
):
    """Simulates the concurrent-approval race: the application pre-check sees no clash."""
    from app.modules.tax.repositories.tax_repository import TaxRepository

    c = await approved_classification(client, admin_headers, admin2_headers)
    a = (await client.post(f"{T}/rules", json=rule_body(c["id"]), headers=admin_headers)).json()
    b = (await client.post(f"{T}/rules", json=rule_body(c["id"], effective_from="2030-06-01"),
                           headers=admin_headers)).json()
    assert (await client.post(f"{T}/rules/{a['id']}/approve", headers=admin2_headers)).status_code == 200

    async def blind(self, *args, **kwargs):
        return None

    monkeypatch.setattr(TaxRepository, "overlapping_approved_rule", blind)
    r = await client.post(f"{T}/rules/{b['id']}/approve", headers=admin2_headers)
    assert r.status_code == 409 and r.json()["code"] == "rule_overlap", r.text


async def test_concurrent_approvals_of_overlapping_rules_leave_exactly_one_approved(
    client, admin_headers, admin2_headers
):
    import asyncio

    c = await approved_classification(client, admin_headers, admin2_headers)
    a = (await client.post(f"{T}/rules", json=rule_body(c["id"]), headers=admin_headers)).json()
    b = (await client.post(f"{T}/rules", json=rule_body(c["id"], effective_from="2030-06-01"),
                           headers=admin_headers)).json()
    results = await asyncio.gather(
        client.post(f"{T}/rules/{a['id']}/approve", headers=admin2_headers),
        client.post(f"{T}/rules/{b['id']}/approve", headers=admin2_headers),
    )
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409], [r.text for r in results]
    assert next(r for r in results if r.status_code == 409).json()["code"] == "rule_overlap"
    approved = await client.get(f"{T}/rules", params={"status": "approved"}, headers=admin_headers)
    assert approved.json()["total"] == 1
