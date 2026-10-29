"""Phase 2.1: shipping configuration is explicit, validated, snapshotted and fail-closed."""

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.core.config import Settings, get_settings
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


@pytest.fixture
async def product(client, admin_headers):
    cat = await client.post(f"{API}/categories", json={"name": "Ship Cat", "description": None}, headers=admin_headers)
    r = await client.post(
        f"{API}/products",
        json={"name": "Ship Item", "description": None, "price_cents": 2000, "sku": "SHIP-1", "stock": 10,
              "category_id": cat.json()["id"], "status": "active"},
        headers=admin_headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _checkout(client, product, qty=1):
    await client.post(f"{API}/cart/items", json={"product_id": product["id"], "quantity": qty})
    return await client.post(
        f"{API}/orders/checkout",
        json={"guest_email": "g@example.com", "shipping_address": ADDR},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )


def _settings(**overrides):
    base = dict(_env_file=None, database_url="postgresql+asyncpg://x", jwt_secret="x" * 32,
                stripe_secret_key="sk_test_x", stripe_webhook_secret="whsec_x")
    return Settings(**{**base, **overrides})


def test_threshold_without_a_basis_is_rejected_at_startup(clean_settings_env):
    with pytest.raises(ValidationError, match="free_shipping_threshold_basis"):
        _settings(free_shipping_threshold_cents=5000)
    ok = _settings(free_shipping_threshold_cents=5000, free_shipping_threshold_basis="subtotal")
    assert ok.free_shipping_threshold_basis == "subtotal"


def test_shipping_settings_default_to_unset_never_to_a_value(clean_settings_env):
    s = _settings()
    assert s.flat_shipping_cents is None
    assert s.free_shipping_threshold_cents is None
    assert s.tax_rate_percent is None


async def test_order_stores_the_shipping_snapshot_and_totals_reconcile(client, product, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "flat_shipping_cents", 4900)
    monkeypatch.setattr(s, "free_shipping_threshold_cents", 10000)
    monkeypatch.setattr(s, "free_shipping_threshold_basis", "subtotal")
    monkeypatch.setattr(s, "tax_rate_percent", Decimal("0"))
    r = await _checkout(client, product)  # subtotal 2000, under threshold
    assert r.status_code == 201, r.text
    o = r.json()
    snap = o["shipping_snapshot"]
    assert o["shipping_cents"] == 4900 == snap["applied_paise"]
    assert snap["flat_charge_paise"] == 4900 and snap["free_threshold_paise"] == 10000
    assert snap["threshold_basis"] == "subtotal" and snap["free_shipping_applied"] is False
    assert snap["tax_treatment"] == "undetermined"
    assert o["total_cents"] == o["subtotal_cents"] + o["shipping_cents"] + o["tax_cents"]


async def test_free_shipping_is_recorded_when_the_threshold_is_met(client, product, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "flat_shipping_cents", 4900)
    monkeypatch.setattr(s, "free_shipping_threshold_cents", 3000)
    monkeypatch.setattr(s, "free_shipping_threshold_basis", "subtotal")
    r = await _checkout(client, product, qty=2)  # subtotal 4000 >= 3000
    o = r.json()
    assert o["shipping_cents"] == 0
    assert o["shipping_snapshot"]["free_shipping_applied"] is True
    assert o["total_cents"] == o["subtotal_cents"]


async def test_changing_settings_later_does_not_alter_a_stored_order(client, product, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "flat_shipping_cents", 4900)
    first = (await _checkout(client, product)).json()
    monkeypatch.setattr(s, "flat_shipping_cents", 9900)
    again = await client.get(f"{API}/orders/guest/{first['id']}")
    assert again.status_code == 200
    assert again.json()["shipping_cents"] == 4900
    assert again.json()["shipping_snapshot"]["flat_charge_paise"] == 4900


async def test_threshold_without_basis_fails_closed_with_no_order_or_stock_change(
    client, db_session, product, monkeypatch
):
    s = get_settings()
    monkeypatch.setattr(s, "free_shipping_threshold_cents", 3000)
    monkeypatch.setattr(s, "free_shipping_threshold_basis", None)
    r = await _checkout(client, product)
    assert r.status_code == 503
    assert (await db_session.execute(select(func.count()).select_from(Order))).scalar_one() == 0
    assert (await client.get(f"{API}/products/{product['id']}", headers=client.admin_headers)).json()["stock"] == 10
