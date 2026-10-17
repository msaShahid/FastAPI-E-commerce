"""
Milestone 1 regression tests: server-authoritative price, required address,
idempotency semantics, and fail-closed shipping/tax. All over real HTTP + Postgres.
"""

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.modules.orders.models.order import Order

API = "/api/v1"
ADDR = {
    "recipient_name": "Test Buyer",
    "line1": "1 Test Street",
    "city": "Testville",
    "state": "MH",
    "postal_code": "400001",
    "country": "IN",
}
OTHER_ADDR = {**ADDR, "line1": "99 Different Road"}


async def _make_product(client, admin_headers, *, price=1000, stock=20, sku="M1-001"):
    cat = await client.post(
        f"{API}/categories", json={"name": f"Cat {sku}", "description": None}, headers=admin_headers
    )
    resp = await client.post(
        f"{API}/products",
        json={
            "name": f"Item {sku}",
            "description": None,
            "price_cents": price,
            "sku": sku,
            "stock": stock,
            "category_id": cat.json()["id"],
            "status": "active",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.fixture
async def product(client, admin_headers):
    return await _make_product(client, admin_headers)


async def _stock(client, product_id):
    return (await client.get(f"{API}/products/{product_id}")).json()["stock"]


async def _order_count(db_session):
    return (await db_session.execute(select(func.count()).select_from(Order))).scalar_one()


async def _add(client, product_id, qty=1):
    r = await client.post(f"{API}/cart/items", json={"product_id": product_id, "quantity": qty})
    assert r.status_code == 201, r.text


def _guest(address=ADDR, email="g@example.com"):
    return {"guest_email": email, "shipping_address": address}


async def _checkout(client, body, key=None):
    return await client.post(
        f"{API}/orders/checkout", json=body, headers={"Idempotency-Key": key or str(uuid.uuid4())}
    )


async def _user(client, name):
    await client.post(
        f"{API}/auth/register",
        json={"username": f"user_{name}", "email": f"{name}@example.com", "password": "UserPass123!"},
    )
    tok = (
        await client.post(
            f"{API}/auth/login", json={"email": f"{name}@example.com", "password": "UserPass123!"}
        )
    ).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


# ---------------------------------------------------------------- price authority


@pytest.mark.parametrize(("new_price", "expected"), [(1500, 1500), (800, 800)])
async def test_price_change_requires_review_then_charges_current_price(
    client, admin_headers, db_session, product, new_price, expected
):
    await _add(client, product["id"])
    await client.patch(f"{API}/products/{product['id']}", json={"price_cents": new_price}, headers=admin_headers)

    first = await _checkout(client, _guest())
    assert first.status_code == 409, first.text
    body = first.json()
    assert body["code"] == "price_changed"
    assert body["new_subtotal_cents"] == expected
    assert body["changes"][0]["old_price_cents"] == 1000
    assert body["changes"][0]["new_price_cents"] == new_price
    assert "request_id" in body
    # nothing was charged, taken or created
    assert await _order_count(db_session) == 0
    assert await _stock(client, product["id"]) == 20

    # the cart was refreshed (and the refresh survived the 409)
    cart = (await client.get(f"{API}/cart")).json()
    assert cart["items"][0]["price_cents_snapshot"] == new_price
    assert cart["items"][0]["price_changed"] is False

    # after review the customer confirms by checking out again: charged the CURRENT price
    second = await _checkout(client, _guest())
    assert second.status_code == 201, second.text
    assert second.json()["subtotal_cents"] == expected
    assert second.json()["items"][0]["price_cents_snapshot"] == expected


async def test_unchanged_price_checks_out_normally(client, product):
    await _add(client, product["id"], 2)
    r = await _checkout(client, _guest())
    assert r.status_code == 201
    assert r.json()["subtotal_cents"] == 2000


# ---------------------------------------------------------------- address required


async def test_empty_body_is_rejected_and_creates_nothing(client, db_session, product):
    headers = await _user(client, "noaddr")
    await client.post(f"{API}/cart/items", json={"product_id": product["id"], "quantity": 1}, headers=headers)
    r = await client.post(f"{API}/orders/checkout", json={}, headers={**headers, "Idempotency-Key": "k-empty"})
    assert r.status_code == 422
    assert await _order_count(db_session) == 0
    assert await _stock(client, product["id"]) == 20


async def test_guest_without_address_is_rejected(client, product):
    await _add(client, product["id"])
    r = await _checkout(client, {"guest_email": "g@example.com"})
    assert r.status_code == 422


async def test_saved_address_checkout_works(client, product):
    headers = await _user(client, "saved")
    addr = await client.post(f"{API}/addresses", json={**ADDR, "is_default": True}, headers=headers)
    await client.post(f"{API}/cart/items", json={"product_id": product["id"], "quantity": 1}, headers=headers)
    r = await client.post(
        f"{API}/orders/checkout",
        json={"address_id": addr.json()["id"]},
        headers={**headers, "Idempotency-Key": "k-saved"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["shipping_address"]["line1"] == ADDR["line1"]


async def test_another_users_saved_address_is_refused(client, product):
    owner = await _user(client, "owner")
    thief = await _user(client, "thief")
    addr = await client.post(f"{API}/addresses", json=ADDR, headers=owner)
    await client.post(f"{API}/cart/items", json={"product_id": product["id"], "quantity": 1}, headers=thief)
    r = await client.post(
        f"{API}/orders/checkout",
        json={"address_id": addr.json()["id"]},
        headers={**thief, "Idempotency-Key": "k-thief"},
    )
    assert r.status_code in (403, 404)


# ---------------------------------------------------------------- idempotency


@pytest.mark.parametrize(("key", "expected"), [("x" * 255, 201), ("x" * 256, 422), ("", 422)])
async def test_idempotency_key_length_is_validated(client, product, key, expected):
    await _add(client, product["id"])
    r = await client.post(f"{API}/orders/checkout", json=_guest(), headers={"Idempotency-Key": key})
    assert r.status_code == expected, r.text


async def test_same_key_same_payload_returns_the_same_order(client, db_session, product):
    await _add(client, product["id"])
    a = await _checkout(client, _guest(), key="same")
    b = await _checkout(client, _guest(), key="same")
    assert a.status_code == b.status_code == 201
    assert a.json()["id"] == b.json()["id"]
    assert await _order_count(db_session) == 1


async def test_same_key_different_payload_is_a_conflict(client, db_session, product):
    await _add(client, product["id"])
    a = await _checkout(client, _guest(), key="reused")
    b = await _checkout(client, _guest(address=OTHER_ADDR), key="reused")
    assert a.status_code == 201
    assert b.status_code == 409
    assert "different request" in b.json()["detail"]
    assert b.json()["code"] == "idempotency_key_reused"
    assert await _order_count(db_session) == 1
    assert await _stock(client, product["id"]) == 19  # taken once


async def test_legacy_order_without_a_hash_still_replays(client, db_session, product):
    """Orders created before request_hash existed have NULL; their replays keep working."""
    await _add(client, product["id"])
    first = await _checkout(client, _guest(), key="legacy")
    await db_session.execute(
        Order.__table__.update().where(Order.id == uuid.UUID(first.json()["id"])).values(request_hash=None)
    )
    await db_session.commit()
    replay = await _checkout(client, _guest(address=OTHER_ADDR), key="legacy")
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]


async def test_concurrent_same_key_creates_exactly_one_order(client, db_session, product):
    await _add(client, product["id"])
    results = await asyncio.gather(*[_checkout(client, _guest(), key="race") for _ in range(5)])
    assert [r.status_code for r in results] == [201] * 5, [r.text for r in results]
    assert len({r.json()["id"] for r in results}) == 1
    assert await _order_count(db_session) == 1
    assert await _stock(client, product["id"]) == 19


# ---------------------------------------------------------------- shipping / tax


@pytest.mark.parametrize("missing", ["flat_shipping_cents", "tax_rate_percent"])
async def test_checkout_fails_closed_when_pricing_is_not_configured(
    client, db_session, product, monkeypatch, missing
):
    monkeypatch.setattr(get_settings(), missing, None)
    await _add(client, product["id"])
    r = await _checkout(client, _guest())
    assert r.status_code == 503
    assert "not configured" in r.json()["detail"]
    assert r.json()["code"] == "checkout_not_configured"
    assert await _order_count(db_session) == 0
    assert await _stock(client, product["id"]) == 20
    # fixing the configuration makes the same cart checkout fine
    monkeypatch.setattr(get_settings(), missing, 0 if missing == "flat_shipping_cents" else Decimal("0"))
    assert (await _checkout(client, _guest())).status_code == 201


@pytest.mark.parametrize(
    ("price", "rate", "shipping", "threshold", "exp_ship", "exp_tax", "exp_total"),
    [
        (1000, "18", 500, 5000, 500, 180, 1680),  # under threshold: shipping applies
        (6000, "18", 500, 5000, 0, 1080, 7080),  # at/over threshold: free shipping
        (1010, "5", 0, None, 0, 51, 1061),  # 50.5 rounds half-up to 51
        (1030, "5", 0, None, 0, 52, 1082),  # 51.5 rounds half-up to 52
    ],
)
async def test_shipping_and_tax_come_from_explicit_settings(
    client, admin_headers, monkeypatch, price, rate, shipping, threshold, exp_ship, exp_tax, exp_total
):
    s = get_settings()
    monkeypatch.setattr(s, "flat_shipping_cents", shipping)
    monkeypatch.setattr(s, "free_shipping_threshold_cents", threshold)
    monkeypatch.setattr(s, "free_shipping_threshold_basis", "subtotal" if threshold else None)
    monkeypatch.setattr(s, "tax_rate_percent", Decimal(rate))
    p = await _make_product(client, admin_headers, price=price, sku=f"ST-{price}-{rate}")
    await _add(client, p["id"])
    r = await _checkout(client, _guest())
    assert r.status_code == 201, r.text
    o = r.json()
    assert (o["shipping_cents"], o["tax_cents"], o["total_cents"]) == (exp_ship, exp_tax, exp_total)
