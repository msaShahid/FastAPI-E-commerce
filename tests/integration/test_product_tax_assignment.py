"""Phase 2.3: assigning approved GST classifications to products. Fictional values only."""

import asyncio
import importlib.util
import pathlib
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.modules.audit.models.audit_log import AuditLog
from app.modules.auth.models.user import User
from app.modules.orders.models.order import Order
from app.modules.tax.models.guard import PRODUCT_TAX_FUNCTIONS
from app.shared.enums.roles import UserRole

API = "/api/v1"
T = f"{API}/admin/tax"
ADDR = {"recipient_name": "R", "line1": "1 Test St", "city": "C", "state": "MH", "postal_code": "400001", "country": "IN"}


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
    r = await client.post(f"{API}/categories", json={"name": "Tax Cat", "description": None}, headers=admin_headers)
    return r.json()["id"]


async def classification(client, h1, h2, code="1234", approve=True, retire=False):
    c = (await client.post(f"{T}/classifications", headers=h1,
                           json={"kind": "hsn", "code": code, "description": "Test goods", "source_reference": "TEST-REF"})).json()
    if approve:
        assert (await client.post(f"{T}/classifications/{c['id']}/approve", headers=h2)).status_code == 200
    if retire:
        assert (await client.post(f"{T}/classifications/{c['id']}/retire", headers=h1)).status_code == 200
    return c["id"]


async def make_product(client, h, category, sku="TP-1", **extra):
    body = {"name": f"Item {sku}", "description": None, "price_cents": 1000, "sku": sku, "stock": 10,
            "category_id": category, "status": "draft", **extra}
    return await client.post(f"{API}/products", json=body, headers=h)


async def _fails(db, sql, **params):
    with pytest.raises(IntegrityError):
        await db.execute(text(sql), params)
    await db.rollback()


# ------------------------------------------------------------------ assignment + visibility
async def test_assign_approved_classification_and_unit_persists_and_is_admin_only(
    client, admin_headers, admin2, category
):
    cid = await classification(client, admin_headers, admin2)
    r = await make_product(client, admin_headers, category, status="active", tax_classification_id=cid, unit="nos")
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["unit"] == "NOS"  # normalised
    assert p["tax_state"] == "assigned_approved"
    assert p["tax_classification"]["id"] == cid and p["tax_classification"]["code"] == "1234"

    admin_view = (await client.get(f"{API}/products/{p['id']}", headers=admin_headers)).json()
    assert admin_view["tax_state"] == "assigned_approved" and admin_view["unit"] == "NOS"

    public = (await client.get(f"{API}/products/{p['id']}")).json()      # anonymous
    assert public["tax_classification"] is None and public["tax_state"] is None and public["unit"] is None
    listing = (await client.get(f"{API}/products")).json()["items"][0]
    assert listing["tax_classification"] is None and listing["tax_state"] is None and listing["unit"] is None


async def test_a_product_without_a_classification_is_unassigned_never_tax_ready(client, admin_headers, category):
    p = (await make_product(client, admin_headers, category)).json()
    assert p["tax_state"] == "unassigned" and p["tax_classification"] is None and p["unit"] is None


async def test_invalid_assignments_are_rejected_with_stable_codes(client, admin_headers, admin2, category):
    missing = await make_product(client, admin_headers, category, sku="A", tax_classification_id=999999)
    assert missing.status_code == 422 and missing.json()["code"] == "tax_classification_invalid"
    draft = await classification(client, admin_headers, admin2, code="1111", approve=False)
    r = await make_product(client, admin_headers, category, sku="B", tax_classification_id=draft)
    assert r.status_code == 422 and r.json()["code"] == "classification_not_approved"
    retired = await classification(client, admin_headers, admin2, code="2222", retire=True)
    r = await make_product(client, admin_headers, category, sku="C", tax_classification_id=retired)
    assert r.status_code == 422 and r.json()["code"] == "classification_not_approved"
    assert (await make_product(client, admin_headers, category, sku="D", tax_classification_id=0)).status_code == 422


@pytest.mark.parametrize("unit", ["k", "kg1", "toolongunit", "k g", ""])
async def test_invalid_unit_is_rejected(client, admin_headers, category, unit):
    r = await make_product(client, admin_headers, category, unit=unit)
    assert r.status_code == 422


async def test_only_administrators_can_assign(client, admin_headers, admin2, category):
    cid = await classification(client, admin_headers, admin2)
    p = (await make_product(client, admin_headers, category)).json()
    await client.post(f"{API}/auth/register",
                      json={"username": "plain_user", "email": "plain@example.com", "password": "UserPass123!"})
    tok = (await client.post(f"{API}/auth/login",
                             json={"email": "plain@example.com", "password": "UserPass123!"})).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert (await client.patch(f"{API}/products/{p['id']}", json={"tax_classification_id": cid}, headers=h)).status_code == 403
    assert (await client.post(f"{API}/products", json={"name": "x", "price_cents": 1, "sku": "Z", "category_id": category,
                                                     "tax_classification_id": cid}, headers=h)).status_code == 403
    assert (await client.patch(f"{API}/products/{p['id']}", json={"tax_classification_id": cid})).status_code in (401, 403)


async def test_patch_assign_change_unassign_and_leave_unchanged_with_audit(
    client, admin_headers, admin2, category, db_session
):
    a = await classification(client, admin_headers, admin2, code="1111")
    b = await classification(client, admin_headers, admin2, code="2222")
    p = (await make_product(client, admin_headers, category)).json()
    url = f"{API}/products/{p['id']}"

    assert (await client.patch(url, json={"tax_classification_id": a}, headers=admin_headers)).json()["tax_classification"]["id"] == a
    # an unrelated edit leaves the assignment alone
    r = await client.patch(url, json={"stock": 5, "stock_reason": "unrelated edit"}, headers=admin_headers)
    assert r.json()["tax_classification"]["id"] == a and r.json()["tax_state"] == "assigned_approved"
    # reassign: the response reflects the NEW classification immediately
    r = await client.patch(url, json={"tax_classification_id": b}, headers=admin_headers)
    assert r.json()["tax_classification"]["id"] == b and r.json()["tax_classification"]["code"] == "2222"
    # explicit null un-assigns
    r = await client.patch(url, json={"tax_classification_id": None}, headers=admin_headers)
    assert r.json()["tax_classification"] is None and r.json()["tax_state"] == "unassigned"

    logs = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "product.tax_assignment_changed").order_by(AuditLog.id))).scalars().all()
    assert [(l.details["from"], l.details["to"]) for l in logs] == [(None, a), (a, b), (b, None)]  # noqa: E741


async def test_a_classification_assigned_to_products_cannot_be_retired(client, admin_headers, admin2, category):
    cid = await classification(client, admin_headers, admin2)
    p = (await make_product(client, admin_headers, category, tax_classification_id=cid)).json()
    r = await client.post(f"{T}/classifications/{cid}/retire", headers=admin_headers)
    assert r.status_code == 409 and r.json()["code"] == "classification_in_use" and "1 product" in r.json()["detail"]
    await client.patch(f"{API}/products/{p['id']}", json={"tax_classification_id": None}, headers=admin_headers)
    assert (await client.post(f"{T}/classifications/{cid}/retire", headers=admin_headers)).status_code == 200


# ------------------------------------------------------------------ database integrity
async def test_database_refuses_invalid_assignments_even_if_the_service_is_bypassed(
    client, admin_headers, admin2, category, db_session
):
    approved = await classification(client, admin_headers, admin2, code="1111")
    draft = await classification(client, admin_headers, admin2, code="2222", approve=False)
    p = (await make_product(client, admin_headers, category, tax_classification_id=approved)).json()

    await _fails(db_session, "UPDATE products SET tax_classification_id = :c WHERE id = :p", c=draft, p=p["id"])
    await _fails(db_session, "UPDATE products SET tax_classification_id = 999999 WHERE id = :p", p=p["id"])
    await _fails(db_session, "UPDATE products SET unit = 'ab1' WHERE id = :p", p=p["id"])
    await _fails(db_session, "UPDATE tax_classifications SET status = 'retired' WHERE id = :c", c=approved)
    await _fails(db_session, "DELETE FROM tax_classifications WHERE id = :c", c=approved)
    await _fails(db_session, """INSERT INTO products (name, slug, sku, price_cents, category_id, tax_classification_id)
                  VALUES ('x', 'x-slug', 'RAW-1', 100, :cat, :c)""", cat=category, c=draft)


async def test_assigning_and_retiring_concurrently_never_leaves_a_product_on_a_retired_classification(
    client, admin_headers, admin2, category, db_session
):
    cid = await classification(client, admin_headers, admin2)
    p = (await make_product(client, admin_headers, category)).json()
    assign, retire = await asyncio.gather(
        client.patch(f"{API}/products/{p['id']}", json={"tax_classification_id": cid}, headers=admin_headers),
        client.post(f"{T}/classifications/{cid}/retire", headers=admin2),
    )
    assert {assign.status_code, retire.status_code} in ({200}, {200, 409}, {200, 422}), (assign.text, retire.text)
    bad = (await db_session.execute(text(
        "SELECT count(*) FROM products p JOIN tax_classifications c ON c.id = p.tax_classification_id "
        "WHERE c.status <> 'approved'"))).scalar_one()
    assert bad == 0


# ------------------------------------------------------------------ unclassified products and checkout
async def _cart_checkout(client, product_ids, key=None):
    for pid in product_ids:
        await client.post(f"{API}/cart/items", json={"product_id": pid, "quantity": 1})
    return await client.post(f"{API}/orders/checkout", json={"guest_email": "g@example.com", "shipping_address": ADDR},
                             headers={"Idempotency-Key": key or str(uuid.uuid4())})


async def test_default_behaviour_is_unchanged_unclassified_products_still_check_out(client, admin_headers, category):
    p = (await make_product(client, admin_headers, category, status="active")).json()
    assert get_settings().tax_classification_required is False
    assert (await _cart_checkout(client, [p["id"]])).status_code == 201


async def test_required_mode_blocks_unclassified_products_without_touching_stock(
    client, admin_headers, admin2, category, db_session, monkeypatch
):
    cid = await classification(client, admin_headers, admin2)
    ok = (await make_product(client, admin_headers, category, sku="OK", status="active", tax_classification_id=cid)).json()
    bad = (await make_product(client, admin_headers, category, sku="BAD", status="active")).json()
    monkeypatch.setattr(get_settings(), "tax_classification_required", True)

    r = await _cart_checkout(client, [ok["id"], bad["id"]])
    assert r.status_code == 422 and r.json()["code"] == "product_tax_unassigned"
    assert str(bad["id"]) in r.json()["detail"] and f"product {ok['id']}," not in r.json()["detail"]
    assert (await db_session.execute(select(func.count()).select_from(Order))).scalar_one() == 0
    assert (await client.get(f"{API}/products/{ok['id']}", headers=client.admin_headers)).json()["stock"] == 10
    assert (await client.get(f"{API}/products/{bad['id']}", headers=client.admin_headers)).json()["stock"] == 10

    items = (await client.get(f"{API}/cart")).json()["items"]
    bad_item = next(i for i in items if i["product"]["id"] == bad["id"])
    await client.delete(f"{API}/cart/items/{bad_item['id']}")
    r = await client.post(f"{API}/orders/checkout", json={"guest_email": "g@example.com", "shipping_address": ADDR},
                          headers={"Idempotency-Key": str(uuid.uuid4())})
    assert r.status_code == 201, r.text  # a cart of classified products is accepted


async def test_required_mode_governs_activation(client, admin_headers, admin2, category, monkeypatch):
    cid = await classification(client, admin_headers, admin2)
    monkeypatch.setattr(get_settings(), "tax_classification_required", True)
    r = await make_product(client, admin_headers, category, sku="N1", status="active")
    assert r.status_code == 422 and r.json()["code"] == "product_tax_classification_required"
    draft = await make_product(client, admin_headers, category, sku="N2", status="draft")
    assert draft.status_code == 201
    url = f"{API}/products/{draft.json()['id']}"
    assert (await client.patch(url, json={"status": "active"}, headers=admin_headers)).status_code == 422
    assert (await client.patch(url, json={"status": "active", "tax_classification_id": cid},
                               headers=admin_headers)).status_code == 200
    # un-assigning an ACTIVE product is refused too; archiving is always allowed
    assert (await client.patch(url, json={"tax_classification_id": None}, headers=admin_headers)).status_code == 422
    assert (await client.patch(url, json={"status": "archived", "tax_classification_id": None},
                               headers=admin_headers)).status_code == 200


# ------------------------------------------------------------------ admin worklist
async def test_admin_can_list_products_by_tax_state(client, admin_headers, admin2, category):
    cid = await classification(client, admin_headers, admin2)
    a = (await make_product(client, admin_headers, category, sku="U1")).json()
    await make_product(client, admin_headers, category, sku="U2")
    c = (await make_product(client, admin_headers, category, sku="C1", tax_classification_id=cid, unit="kgs")).json()

    un = (await client.get(f"{T}/products", params={"state": "unassigned"}, headers=admin_headers)).json()
    assert un["total"] == 2 and all(i["tax_state"] == "unassigned" for i in un["items"])
    ap = (await client.get(f"{T}/products", params={"state": "assigned_approved"}, headers=admin_headers)).json()
    assert [i["id"] for i in ap["items"]] == [c["id"]] and ap["items"][0]["unit"] == "KGS"
    assert (await client.get(f"{T}/products", params={"state": "assigned_unapproved"}, headers=admin_headers)).json()["total"] == 0
    assert (await client.get(f"{T}/products", headers=admin_headers)).json()["total"] == 3
    page = (await client.get(f"{T}/products", params={"page": 1, "page_size": 1}, headers=admin_headers)).json()
    assert len(page["items"]) == 1 and page["total"] == 3
    assert a["id"] in [i["id"] for i in un["items"]]


async def test_tax_worklist_requires_an_administrator(client):
    assert (await client.get(f"{T}/products")).status_code in (401, 403)


def test_migration_and_model_use_identical_product_trigger_sql():
    path = next(pathlib.Path("alembic/versions").glob("b8d4f1a6c230_*.py"))
    spec = importlib.util.spec_from_file_location("product_tax_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    migration = (module.PRODUCT_ASSIGNMENT_GUARD_SQL.strip(), module.CLASSIFICATION_RETIRE_GUARD_SQL.strip())
    assert migration == tuple(s.strip() for s in PRODUCT_TAX_FUNCTIONS)
