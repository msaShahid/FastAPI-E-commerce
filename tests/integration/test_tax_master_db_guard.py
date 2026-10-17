"""
Database-level immutability of the GST master. These go straight to SQL on purpose:
they prove the guarantee holds even when the service layer is bypassed.
"""

import importlib.util
import pathlib
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.modules.auth.models.user import User
from app.modules.tax.models.guard import GUARD_FUNCTION_SQL
from app.shared.enums.roles import UserRole

T = "/api/v1/admin/tax"


def make_gstin():
    return "27ABCDE1234F1Z5"  # fictional, format-valid


@pytest.fixture
async def admin2_headers(client, db_session):
    await client.post("/api/v1/auth/register",
                      json={"username": "admin_two", "email": "admin2@example.com", "password": "AdminPassword123!"})
    tok = (await client.post("/api/v1/auth/login",
                             json={"email": "admin2@example.com", "password": "AdminPassword123!"})).json()["access_token"]
    await db_session.execute(User.__table__.update().where(User.email == "admin2@example.com").values(role=UserRole.ADMIN))
    await db_session.commit()
    return {"Authorization": f"Bearer {tok}"}


async def approved_classification(client, h1, h2, code="1234"):
    c = (await client.post(f"{T}/classifications", headers=h1,
                           json={"kind": "hsn", "code": code, "description": "Test goods", "source_reference": "TEST-REF"})).json()
    assert (await client.post(f"{T}/classifications/{c['id']}/approve", headers=h2)).status_code == 200
    return (await client.get(f"{T}/classifications", params={"status": "approved"}, headers=h1)).json()["items"][0]


def rule_body(cid, **over):
    body = {"classification_id": cid, "treatment": "taxable", "cgst_rate": "2.5", "sgst_utgst_rate": "2.5",
            "igst_rate": "5", "effective_from": "2030-01-01", "source_reference": "TEST-NOTIF-1"}
    return {**body, **over}


async def _rule(client, h1, h2, **over):
    c = await approved_classification(client, h1, h2)
    r = (await client.post(f"{T}/rules", json=rule_body(c["id"], **over), headers=h1)).json()
    assert (await client.post(f"{T}/rules/{r['id']}/approve", headers=h2)).status_code == 200
    return c, r


async def _fails(db, sql, **params):
    with pytest.raises(IntegrityError):
        await db.execute(text(sql), params)
    await db.rollback()


async def test_records_cannot_be_inserted_already_approved(client, admin_headers, admin2_headers, db_session):
    c = await approved_classification(client, admin_headers, admin2_headers)
    await _fails(db_session, """INSERT INTO tax_classifications (kind, code, description, status, source_reference,
        approved_at) VALUES ('hsn', '9999', 'x', 'approved', 'R', now())""")
    await _fails(db_session, """INSERT INTO tax_rules (classification_id, treatment, cgst_rate, sgst_utgst_rate,
        igst_rate, effective_from, status, source_reference, approved_at)
        VALUES (:c, 'taxable', 2.5, 2.5, 5, :f, 'approved', 'R', now())""", c=c["id"], f=date(2040, 1, 1))


async def test_approved_classification_is_read_only_except_retiring(
    client, admin_headers, admin2_headers, db_session
):
    c = await approved_classification(client, admin_headers, admin2_headers)
    await _fails(db_session, "UPDATE tax_classifications SET description = 'changed' WHERE id = :i", i=c["id"])
    await _fails(db_session, "UPDATE tax_classifications SET code = '4321' WHERE id = :i", i=c["id"])
    await _fails(db_session, "UPDATE tax_classifications SET source_reference = 'other' WHERE id = :i", i=c["id"])
    await _fails(db_session, "UPDATE tax_classifications SET status = 'draft' WHERE id = :i", i=c["id"])
    await _fails(db_session, "DELETE FROM tax_classifications WHERE id = :i", i=c["id"])
    await db_session.execute(text("UPDATE tax_classifications SET status = 'retired' WHERE id = :i"), {"i": c["id"]})
    await db_session.commit()
    await _fails(db_session, "UPDATE tax_classifications SET description = 'changed' WHERE id = :i", i=c["id"])
    await _fails(db_session, "UPDATE tax_classifications SET status = 'approved' WHERE id = :i", i=c["id"])
    await _fails(db_session, "DELETE FROM tax_classifications WHERE id = :i", i=c["id"])


@pytest.mark.parametrize("column_sql", [
    "igst_rate = 12, cgst_rate = 6, sgst_utgst_rate = 6",
    "treatment = 'exempt', igst_rate = 0, cgst_rate = 0, sgst_utgst_rate = 0",
    "effective_from = '2029-01-01'",
    "source_reference = 'rewritten'",
    "classification_id = classification_id + 1000",
])
async def test_approved_rule_cannot_be_rewritten(client, admin_headers, admin2_headers, db_session, column_sql):
    _, r = await _rule(client, admin_headers, admin2_headers)
    await _fails(db_session, f"UPDATE tax_rules SET {column_sql} WHERE id = :i", i=r["id"])
    await _fails(db_session, "DELETE FROM tax_rules WHERE id = :i", i=r["id"])


async def test_approved_rule_end_date_can_only_move_forward_in_time_never_extend(
    client, admin_headers, admin2_headers, db_session
):
    _, open_ended = await _rule(client, admin_headers, admin2_headers)  # effective_to NULL
    await db_session.execute(text("UPDATE tax_rules SET effective_to = '2031-01-01' WHERE id = :i"), {"i": open_ended["id"]})
    await db_session.commit()                                          # NULL -> date: allowed
    await db_session.execute(text("UPDATE tax_rules SET effective_to = '2030-06-01' WHERE id = :i"), {"i": open_ended["id"]})
    await db_session.commit()                                          # shortened: allowed
    await _fails(db_session, "UPDATE tax_rules SET effective_to = '2032-01-01' WHERE id = :i", i=open_ended["id"])
    await _fails(db_session, "UPDATE tax_rules SET effective_to = NULL WHERE id = :i", i=open_ended["id"])


async def test_approved_registration_is_read_only_except_retiring(
    client, admin_headers, admin2_headers, db_session
):
    reg = (await client.post(f"{T}/registrations", headers=admin_headers, json={
        "gstin": make_gstin(), "legal_name": "Test Business", "state_code": "27", "valid_from": "2030-01-01"})).json()
    await client.post(f"{T}/registrations/{reg['id']}/approve", headers=admin2_headers)
    await _fails(db_session, "UPDATE gst_registrations SET legal_name = 'Other' WHERE id = :i", i=reg["id"])
    await _fails(db_session, "UPDATE gst_registrations SET valid_to = '2031-01-01' WHERE id = :i", i=reg["id"])
    await _fails(db_session, "DELETE FROM gst_registrations WHERE id = :i", i=reg["id"])
    await db_session.execute(text("UPDATE gst_registrations SET status = 'retired' WHERE id = :i"), {"i": reg["id"]})
    await db_session.commit()


async def test_drafts_stay_editable_and_deletable(client, admin_headers, db_session):
    d = (await client.post(f"{T}/classifications", headers=admin_headers,
                           json={"kind": "hsn", "code": "8888", "description": "x"})).json()
    await db_session.execute(text("UPDATE tax_classifications SET description = 'edited' WHERE id = :i"), {"i": d["id"]})
    await db_session.execute(text("DELETE FROM tax_classifications WHERE id = :i"), {"i": d["id"]})
    await db_session.commit()


async def test_deleting_a_user_can_clear_but_not_reassign_the_creator(
    client, admin_headers, admin2_headers, db_session
):
    c = await approved_classification(client, admin_headers, admin2_headers)
    ids = (await db_session.execute(text("SELECT id FROM users ORDER BY created_at"))).scalars().all()
    other = next(i for i in ids if str(i) != c["created_by_user_id"])
    await _fails(db_session, "UPDATE tax_classifications SET created_by_user_id = :u WHERE id = :i", u=other, i=c["id"])
    # the FK's ON DELETE SET NULL path (clearing) must still work for an approved record
    await db_session.execute(text("UPDATE tax_classifications SET created_by_user_id = NULL WHERE id = :i"), {"i": c["id"]})
    await db_session.commit()


async def test_the_service_flows_still_pass_through_the_guard(client, admin_headers, admin2_headers):
    """create -> approve -> close -> successor approve -> retire all go through the trigger."""
    c, first = await _rule(client, admin_headers, admin2_headers)
    assert (await client.post(f"{T}/rules/{first['id']}/close", json={"effective_to": "2031-01-01"},
                              headers=admin_headers)).status_code == 200
    nxt = (await client.post(f"{T}/rules", json=rule_body(c["id"], effective_from="2031-01-01"),
                             headers=admin_headers)).json()
    assert (await client.post(f"{T}/rules/{nxt['id']}/approve", headers=admin2_headers)).status_code == 200
    for rid in (first["id"], nxt["id"]):
        assert (await client.post(f"{T}/rules/{rid}/retire", headers=admin_headers)).status_code == 200
    assert (await client.post(f"{T}/classifications/{c['id']}/retire", headers=admin_headers)).status_code == 200


def test_migration_and_model_use_identical_guard_sql():
    path = next(pathlib.Path("alembic/versions").glob("f2b6d8a4c917_*.py"))
    spec = importlib.util.spec_from_file_location("guard_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.GUARD_FUNCTION_SQL.strip() == GUARD_FUNCTION_SQL.strip()
