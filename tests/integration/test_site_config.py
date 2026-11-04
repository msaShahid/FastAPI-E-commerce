"""Storefront configuration: a draft nobody sees, versions published and rolled back, strict validation, and the storefront told on publish."""

import copy

import pytest
from sqlalchemy import text
from test_cart_variants import signup

from app.core import revalidate

API = "/api/v1"
DOC = {
    "settings": {
        "site": {"name": "Shop One", "tagline": "Good things"},
        "display": {"formatLocale": "en-IN", "symbolStyle": "symbol"},
        "navigation": {"header": [{"label": "Home", "href": "/"}, {"label": "Sale", "href": "/categories/sale-3", "visible": False}], "footer": []},
        "announcement": {"visible": True, "text": "Free delivery this week", "link": {"label": "Shop", "href": "/products"}},
        "countries": ["in", "us"],
    },
    "sections": {"home": [
        {"id": "hero", "type": "hero", "order": 1, "data": {"headline": "Welcome", "primary": {"label": "Browse", "href": "/products"}}},
        {"id": "row", "type": "product_row", "order": 2, "data": {"title": "Picks", "productIds": [1, 2], "limit": 2}},
    ]},
    "flags": {"cart": True, "reviews": False},
}


@pytest.fixture
def announced(monkeypatch):
    sent = []
    monkeypatch.setattr(revalidate, "schedule_notify", lambda tags: sent.append(sorted(tags)))
    return sent


async def save(client, headers, doc=DOC):
    return await client.put(f"{API}/admin/site-config/draft", json=doc, headers=headers)


async def publish(client, headers, **body):
    return await client.post(f"{API}/admin/site-config/publish", json=body, headers=headers)


async def public(client):
    r = await client.get(f"{API}/site-config")
    assert r.status_code == 200, r.text
    return r.json()


async def test_nothing_published_yet_gives_an_empty_envelope_the_storefront_fills_from_its_defaults(client):
    r = await client.get(f"{API}/site-config")
    assert r.json() == {"schema_version": 1, "config_version": None, "settings": {}, "sections": {}, "flags": {}}
    assert "max-age" in r.headers["cache-control"]


async def test_a_draft_is_invisible_until_published_then_the_storefront_sees_exactly_it(client, admin_headers, announced):
    r = await save(client, admin_headers)
    assert r.status_code == 200 and r.json()["draft_differs_from_published"] is True and r.json()["published"] is None
    assert (await public(client))["config_version"] is None  # still the defaults
    assert announced == []  # saving a draft changes nothing the storefront can see

    r = await publish(client, admin_headers, note="First")
    assert r.status_code == 200 and r.json()["version"] == 1 and r.json()["is_published"] is True
    body = await public(client)
    assert body["config_version"] == 1 and body["schema_version"] == 1
    assert body["settings"]["site"] == {"name": "Shop One", "tagline": "Good things"}
    assert body["settings"]["display"] == {"formatLocale": "en-IN", "symbolStyle": "symbol"}  # the storefront's own key names
    assert body["settings"]["countries"] == ["IN", "US"]
    assert [s["id"] for s in body["sections"]["home"]] == ["hero", "row"]
    assert body["sections"]["home"][1]["data"] == {"title": "Picks", "productIds": [1, 2], "limit": 2}
    assert body["flags"] == {"cart": True, "reviews": False}
    assert announced == [["site-config"]]  # told once, after the commit
    overview = (await client.get(f"{API}/admin/site-config", headers=admin_headers)).json()
    assert overview["draft"] is None and overview["published"]["version"] == 1  # the draft became the version


async def test_every_admin_endpoint_needs_an_administrator(client, admin_headers):
    user, _ = await signup(client)
    calls = [("GET", "/admin/site-config", None), ("PUT", "/admin/site-config/draft", DOC), ("DELETE", "/admin/site-config/draft", None),
             ("POST", "/admin/site-config/publish", {}), ("GET", "/admin/site-config/versions", None), ("GET", "/admin/site-config/versions/1", None),
             ("POST", "/admin/site-config/rollback/1", {})]
    for method, path, body in calls:
        assert (await client.request(method, f"{API}{path}", json=body)).status_code == 401, path
        assert (await client.request(method, f"{API}{path}", json=body, headers=user)).status_code == 403, path


def with_(path, value, doc=DOC):
    d = copy.deepcopy(doc)
    target = d
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return d


@pytest.mark.parametrize("bad", [
    with_(["settings", "navigation", "header", 0, "href"], "javascript:alert(1)"),
    with_(["settings", "navigation", "header", 0, "href"], "//evil.test"),
    with_(["settings", "navigation", "header", 0, "href"], "data:text/html,x"),
    with_(["settings", "navigation", "header", 0, "href"], "/a\\b"),
    with_(["settings", "site", "name"], ""),
    with_(["settings", "site", "name"], "x" * 61),
    with_(["settings", "countries"], ["IN", "in"]),
    with_(["settings", "countries"], ["INDIA"]),
    with_(["settings", "display", "symbolStyle"], "fancy"),
    with_(["settings", "unknown_namespace"], {}),
    with_(["settings", "site", "colour"], "red"),
    with_(["flags", "teleporter"], True),
    with_(["flags", "cart"], "yes"),
    with_(["sections", "home", 0, "type"], "carousel"),
    with_(["sections", "home", 0, "data"], {"headline": "x" * 121}),
    with_(["sections", "home", 1, "data", "limit"], 99),
    with_(["sections", "home", 1, "id"], "hero"),
    with_(["sections", "home", 0, "schema_version"], 2),
    with_(["settings", "announcement", "link", "href"], "ftp://files"),
])
async def test_the_draft_is_validated_against_what_the_storefront_can_render(client, admin_headers, bad):
    assert (await save(client, admin_headers, bad)).status_code == 422
    assert (await client.get(f"{API}/admin/site-config", headers=admin_headers)).json()["draft"] is None  # nothing was saved


async def test_publishing_needs_a_draft_a_change_and_an_up_to_date_page(client, admin_headers):
    r = await publish(client, admin_headers)
    assert r.status_code == 422 and r.json()["code"] == "site_config_no_draft"
    await save(client, admin_headers)
    assert (await publish(client, admin_headers, expected_published_version=0)).status_code == 200
    await save(client, admin_headers)  # the very same document again
    r = await publish(client, admin_headers)
    assert r.status_code == 422 and r.json()["code"] == "site_config_unchanged"
    await save(client, admin_headers, with_(["settings", "site", "name"], "Shop Two"))
    r = await publish(client, admin_headers, expected_published_version=0)  # this admin's page is stale: version 1 exists
    assert r.status_code == 409 and r.json()["code"] == "site_config_changed"
    assert (await publish(client, admin_headers, expected_published_version=1)).json()["version"] == 2


async def test_rolling_back_publishes_a_copy_as_a_new_version_and_history_is_kept(client, admin_headers, announced, db_session):
    await save(client, admin_headers)
    await publish(client, admin_headers, note="One")
    await save(client, admin_headers, with_(["settings", "site", "name"], "Shop Two"))
    await publish(client, admin_headers, note="Two")
    announced.clear()

    r = await client.post(f"{API}/admin/site-config/rollback/1", json={}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["version"] == 3 and r.json()["note"] == "Rollback to version 1"
    body = await public(client)
    assert body["config_version"] == 3 and body["settings"]["site"]["name"] == "Shop One"
    assert announced == [["site-config"]]

    versions = (await client.get(f"{API}/admin/site-config/versions", headers=admin_headers)).json()
    assert [(v["version"], v["is_published"]) for v in versions["items"]] == [(3, True), (2, False), (1, False)] and versions["total"] == 3
    assert (await client.get(f"{API}/admin/site-config/versions/2", headers=admin_headers)).json()["document"]["settings"]["site"]["name"] == "Shop Two"
    assert (await client.post(f"{API}/admin/site-config/rollback/3", json={}, headers=admin_headers)).json()["code"] == "site_config_unchanged"
    assert (await client.post(f"{API}/admin/site-config/rollback/99", json={}, headers=admin_headers)).status_code == 404
    actions = (await db_session.execute(text("SELECT action FROM audit_logs WHERE action LIKE 'site_config.%' ORDER BY created_at"))).scalars().all()
    assert actions.count("site_config.published") == 2 and actions.count("site_config.rolled_back") == 1 and "site_config.draft_saved" in actions


async def test_discarding_a_draft_leaves_the_published_version_alone(client, admin_headers):
    await save(client, admin_headers)
    await publish(client, admin_headers)
    await save(client, admin_headers, with_(["flags", "cart"], False))
    r = await client.delete(f"{API}/admin/site-config/draft", headers=admin_headers)
    assert r.status_code == 200 and r.json()["draft"] is None and r.json()["published"]["version"] == 1
    assert (await public(client))["flags"]["cart"] is True
