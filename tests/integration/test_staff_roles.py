"""BE-P1: staff roles and permissions, end to end through the API."""

import pytest
from sqlalchemy import text
from test_cart_variants import signup
from test_order_payment_lifecycle import make_order

from app.modules.roles.permissions import OWNER_ROLE_ID, Permission

API = "/api/v1"


async def make_admin(client, db_session):
    headers, email = await signup(client)
    await db_session.execute(text("UPDATE users SET role = 'admin' WHERE email = :e"), {"e": email})
    await db_session.commit()
    me = (await client.get(f"{API}/users/me", headers=headers)).json()
    return headers, me["id"]


async def new_role(client, headers, name, permissions):
    r = await client.post(f"{API}/admin/roles", json={"name": name, "permissions": permissions}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


async def assign(client, headers, user_id, role_id):
    return await client.put(f"{API}/admin/staff/{user_id}/role", json={"role_id": role_id}, headers=headers)


@pytest.fixture
async def owner(client, db_session, admin_headers):
    """The fixture's administrator has no role: a legacy administrator with every permission."""
    return admin_headers


async def test_access_me_for_a_customer_and_for_an_administrator_without_a_role(client, owner):
    customer, _ = await signup(client)
    r = await client.get(f"{API}/access/me", headers=customer)
    assert r.status_code == 200 and r.json() == {"is_staff": False, "role": None, "permissions": []}
    assert r.headers["cache-control"] == "no-store"

    body = (await client.get(f"{API}/access/me", headers=owner)).json()
    assert body["is_staff"] is True and body["role"] is None
    assert body["permissions"] == sorted(p.value for p in Permission)


async def test_a_custom_role_allows_only_its_permissions(client, db_session, owner):
    staff, staff_id = await make_admin(client, db_session)
    role = await new_role(client, owner, "Analyst", ["reports.read"])
    assert (await assign(client, owner, staff_id, role["id"])).status_code == 200

    assert (await client.get(f"{API}/admin/reports/summary", headers=staff)).status_code == 200
    r = await client.get(f"{API}/admin/coupons", headers=staff)
    assert r.status_code == 403
    assert r.json()["code"] == "permission_denied" and r.json()["permission"] == "coupons.manage"
    me = (await client.get(f"{API}/access/me", headers=staff)).json()
    assert me["role"]["name"] == "Analyst" and me["permissions"] == ["reports.read"]

    # inline checks: the customer list and another customer's order
    assert (await client.get(f"{API}/users", headers=staff)).json()["code"] == "permission_denied"


async def test_staff_without_orders_read_cannot_open_another_customers_order(client, db_session, owner):
    category = (await client.post(f"{API}/categories", json={"name": "Roles Cat", "description": None}, headers=owner)).json()["id"]
    _, order, _ = await make_order(client, owner, category)
    staff, staff_id = await make_admin(client, db_session)
    role = await new_role(client, owner, "Catalogue", ["catalogue.write"])
    await assign(client, owner, staff_id, role["id"])
    assert (await client.get(f"{API}/orders/{order['id']}", headers=staff)).status_code in (403, 404)
    assert (await client.get(f"{API}/orders/{order['id']}", headers=owner)).status_code == 200


async def test_a_customer_still_gets_the_old_403_on_admin_routes(client, owner):
    customer, _ = await signup(client)
    r = await client.get(f"{API}/admin/reports/summary", headers=customer)
    assert r.status_code == 403 and "code" not in r.json()


async def test_the_owner_role_cannot_be_changed_or_deleted(client, owner):
    roles = (await client.get(f"{API}/admin/roles", headers=owner)).json()
    owner_role = next(r for r in roles if r["id"] == str(OWNER_ROLE_ID))
    assert owner_role["is_system"] and owner_role["permissions"] == sorted(p.value for p in Permission)
    r = await client.patch(f"{API}/admin/roles/{OWNER_ROLE_ID}", json={"name": "Boss"}, headers=owner)
    assert r.status_code == 422 and r.json()["code"] == "role_is_system"
    r = await client.delete(f"{API}/admin/roles/{OWNER_ROLE_ID}", headers=owner)
    assert r.status_code == 422 and r.json()["code"] == "role_is_system"


async def test_role_names_are_unique_and_a_role_in_use_cannot_be_deleted(client, db_session, owner):
    role = await new_role(client, owner, "Support", ["orders.read"])
    r = await client.post(f"{API}/admin/roles", json={"name": "  support ", "permissions": []}, headers=owner)
    assert r.status_code == 409 and r.json()["code"] == "role_name_taken"
    _, staff_id = await make_admin(client, db_session)
    await assign(client, owner, staff_id, role["id"])
    r = await client.delete(f"{API}/admin/roles/{role['id']}", headers=owner)
    assert r.status_code == 409 and r.json() == {**r.json(), "code": "role_in_use", "members": 1}


async def test_an_unknown_permission_is_refused(client, owner):
    r = await client.post(f"{API}/admin/roles", json={"name": "X", "permissions": ["everything.ever"]}, headers=owner)
    assert r.status_code == 422


async def test_the_last_role_manager_cannot_lose_the_permission(client, db_session, owner):
    me = (await client.get(f"{API}/users/me", headers=owner)).json()
    limited = await new_role(client, owner, "Limited", ["reports.read"])
    r = await assign(client, owner, me["id"], limited["id"])
    assert r.status_code == 409 and r.json()["code"] == "last_role_manager"

    managers = await new_role(client, owner, "Managers", ["roles.manage"])
    assert (await assign(client, owner, me["id"], managers["id"])).status_code == 200
    r = await client.patch(f"{API}/admin/roles/{managers['id']}", json={"permissions": ["reports.read"]}, headers=owner)
    assert r.status_code == 409 and r.json()["code"] == "last_role_manager"

    # demoting the only administrator who can manage roles is refused too
    staff, staff_id = await make_admin(client, db_session)
    await assign(client, owner, staff_id, limited["id"])
    r = await client.patch(f"{API}/users/{me['id']}", json={"role": "user"}, headers=owner)
    assert r.status_code == 409 and r.json()["code"] == "last_role_manager"


async def test_demotion_drops_the_role_and_every_change_is_audited(client, db_session, owner):
    role = await new_role(client, owner, "Temp", ["reports.read"])
    staff, staff_id = await make_admin(client, db_session)
    await assign(client, owner, staff_id, role["id"])
    assert (await client.patch(f"{API}/users/{staff_id}", json={"role": "user"}, headers=owner)).status_code == 200
    left = await db_session.scalar(text("SELECT count(*) FROM staff_role_assignments WHERE user_id = :u"), {"u": staff_id})
    assert left == 0
    actions = set(await db_session.scalars(text("SELECT action FROM audit_logs")))
    assert {"staff_role.created", "staff.role_assigned", "user.role_changed"} <= actions


async def test_assigning_a_role_to_a_customer_is_refused(client, owner):
    role = await new_role(client, owner, "Any", [])
    _, email = await signup(client)
    customer_id = (await client.get(f"{API}/users", headers=owner)).json()["items"]
    target = next(u["id"] for u in customer_id if u["email"] == email)
    r = await assign(client, owner, target, role["id"])
    assert r.status_code == 404 and r.json()["code"] == "staff_member_not_found"


async def test_someone_promoted_through_the_api_starts_with_no_access(client, owner):
    headers, email = await signup(client)
    user_id = (await client.get(f"{API}/users/me", headers=headers)).json()["id"]
    assert (await client.patch(f"{API}/users/{user_id}", json={"role": "admin"}, headers=owner)).status_code == 200
    me = (await client.get(f"{API}/access/me", headers=headers)).json()
    assert me["is_staff"] is True and me["role"]["name"] == "No access" and me["permissions"] == []
    r = await client.get(f"{API}/admin/reports/summary", headers=headers)
    assert r.status_code == 403 and r.json()["code"] == "permission_denied"
    r = await client.patch(f"{API}/admin/roles/6f0c1e2a-0000-4000-8000-000000000002", json={"permissions": ["reports.read"]}, headers=owner)
    assert r.status_code == 422 and r.json()["code"] == "role_is_system"


async def test_a_change_permission_includes_its_read_and_drafts_need_catalogue_access(client, db_session, owner):
    category = (await client.post(f"{API}/categories", json={"name": "Draft Cat", "description": None}, headers=owner)).json()["id"]
    product = (await client.post(f"{API}/products", headers=owner, json={
        "name": "Draft thing", "description": None, "price_cents": 500, "sku": "DRAFT-1", "stock": 3,
        "category_id": category, "status": "draft"})).json()
    editor, editor_id = await make_admin(client, db_session)
    analyst, analyst_id = await make_admin(client, db_session)
    await assign(client, owner, editor_id, (await new_role(client, owner, "Editor", ["catalogue.write"]))["id"])
    await assign(client, owner, analyst_id, (await new_role(client, owner, "Numbers", ["reports.read"]))["id"])

    role = next(r for r in (await client.get(f"{API}/admin/roles", headers=owner)).json() if r["name"] == "Editor")
    assert role["permissions"] == ["catalogue.write"] and role["effective_permissions"] == ["catalogue.read", "catalogue.write"]
    assert (await client.get(f"{API}/products/{product['id']}", headers=editor)).status_code == 200
    assert (await client.get(f"{API}/products/{product['id']}", headers=analyst)).status_code == 404
