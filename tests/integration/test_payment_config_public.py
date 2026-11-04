# ruff: noqa: F811  (fixtures imported from other suites are re-declared as test parameters)
"""GET /store/payment-config: the public key a browser needs for the gateway a new payment would use, and nothing secret."""

import json

from sqlalchemy import text
from test_payment_modes import add_stripe, enable_demo, world  # noqa: F401

from app.core.config import get_settings

API = "/api/v1"
SECRET = "sk_test_DISTINCTIVE_SECRET_FOR_PK_TESTS_1"
PK = "pk_test_PUBLISHABLE_KEY_FOR_TESTS_123456"


async def config(client):
    r = await client.get(f"{API}/store/payment-config")
    assert r.status_code == 200, r.text
    return r.json()


async def select_stripe(client, admin_headers):
    cfg = await add_stripe(client, admin_headers, "test", SECRET, "whsec_DISTINCTIVE_WEBHOOK_FOR_PK_TESTS_1")
    cid = cfg["id"]
    assert (await client.post(f"{API}/admin/payment-providers/{cid}/validate", headers=admin_headers)).json()["valid"] is True
    for step in ("enable", "prefer"):
        assert (await client.post(f"{API}/admin/payment-providers/{cid}/{step}", headers=admin_headers)).status_code == 200
    return cid


async def test_with_no_gateway_nothing_is_offered(client, world, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "stripe_secret_key", "")  # no environment Stripe either
    body = await config(client)
    assert body == {"provider": None, "mode": "none", "demo": False, "publishable_key": None, "browser_ready": False, "currencies": []}


async def test_the_demo_gateway_is_ready_and_needs_no_key(client, admin_headers, world):
    await enable_demo(client, admin_headers)
    body = await config(client)
    assert (body["provider"], body["mode"], body["demo"], body["browser_ready"], body["publishable_key"]) == ("demo", "demo", True, True, None)
    assert (await client.get(f"{API}/store/settings")).json()["payment_mode"] == body["mode"]  # the two can never disagree


async def test_stripe_needs_its_publishable_key_before_a_browser_can_pay_and_no_secret_is_ever_shown(client, admin_headers, world):
    cid = await select_stripe(client, admin_headers)
    before = await config(client)
    assert (before["provider"], before["mode"], before["browser_ready"], before["publishable_key"]) == ("stripe", "test", False, None)

    r = await client.put(f"{API}/admin/payment-providers/{cid}/publishable-key", json={"publishable_key": PK}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["credentials"]["public_identifier"] == PK
    after = await config(client)
    assert (after["browser_ready"], after["publishable_key"], after["currencies"]) == (True, PK, ["usd"])
    everything = json.dumps([before, after, r.json(), (await client.get(f"{API}/store/settings")).json()])
    assert "DISTINCTIVE_SECRET" not in everything and "DISTINCTIVE_WEBHOOK" not in everything and "sk_test_" not in everything


async def test_a_publishable_key_must_belong_to_the_configurations_mode_and_to_stripe(client, admin_headers, world):
    cid = await select_stripe(client, admin_headers)
    url = f"{API}/admin/payment-providers/{cid}/publishable-key"
    for bad in ("pk_live_NOT_FOR_A_TEST_CONFIG_1234", "sk_test_THIS_IS_A_SECRET_NOT_PUBLISHABLE", "garbage-garbage-garbage"):
        r = await client.put(url, json={"publishable_key": bad}, headers=admin_headers)
        assert r.status_code == 422 and r.json()["code"] == "credentials_mode_mismatch", bad
    assert (await config(client))["publishable_key"] is None
    # Razorpay's public id comes with its credentials: there is nothing to set separately
    razorpay = (await client.post(f"{API}/admin/payment-providers", headers=admin_headers, json={
        "provider": "razorpay", "mode": "test", "credentials": {"key_id": "rzp_test_ABCDEFGH12345", "key_secret": "SECRET_VALUE_SHOULD_NEVER_SHOW"}, "currencies": ["inr"]})).json()
    r = await client.put(f"{API}/admin/payment-providers/{razorpay['id']}/publishable-key", json={"publishable_key": PK}, headers=admin_headers)
    assert r.status_code == 422 and r.json()["code"] == "publishable_key_not_applicable"


async def test_rotating_the_secret_keeps_the_saved_publishable_key(client, admin_headers, world):
    cid = await select_stripe(client, admin_headers)
    await client.put(f"{API}/admin/payment-providers/{cid}/publishable-key", json={"publishable_key": PK}, headers=admin_headers)
    r = await client.put(f"{API}/admin/payment-providers/{cid}/credentials", json={"credentials": {"secret_key": "sk_test_ANOTHER_SECRET_AFTER_ROTATION_1"}}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["credentials"]["public_identifier"] == PK


async def test_razorpays_public_key_id_is_offered_and_its_secret_never_is(client, admin_headers, world, db_session):
    created = (await client.post(f"{API}/admin/payment-providers", headers=admin_headers, json={
        "provider": "razorpay", "mode": "test", "credentials": {"key_id": "rzp_test_ABCDEFGH12345", "key_secret": "SECRET_VALUE_SHOULD_NEVER_SHOW"}, "currencies": ["inr"]})).json()
    await db_session.execute(text("UPDATE payment_provider_configs SET status = 'enabled', credentials_status = 'valid', is_preferred = true WHERE id = :i"), {"i": created["id"]})
    await db_session.commit()
    body = await config(client)
    assert (body["provider"], body["mode"], body["publishable_key"], body["browser_ready"]) == ("razorpay", "test", "rzp_test_ABCDEFGH12345", True)
    assert "SECRET_VALUE_SHOULD_NEVER_SHOW" not in json.dumps(body)


async def test_the_environment_stripe_is_used_only_when_no_configuration_exists(client, world, monkeypatch):
    monkeypatch.setattr(get_settings(), "stripe_secret_key", "sk_test_ENVIRONMENT_STRIPE_KEY_FOR_TEST")
    monkeypatch.setattr(get_settings(), "stripe_publishable_key", None)
    body = await config(client)
    assert (body["provider"], body["mode"], body["browser_ready"], body["publishable_key"]) == ("stripe", "test", False, None)
    monkeypatch.setattr(get_settings(), "stripe_publishable_key", PK)
    assert (await config(client))["publishable_key"] == PK


async def test_saving_a_publishable_key_needs_an_administrator(client, admin_headers):
    r = await client.put(f"{API}/admin/payment-providers/00000000-0000-0000-0000-000000000000/publishable-key", json={"publishable_key": PK})
    assert r.status_code in (401, 403)
