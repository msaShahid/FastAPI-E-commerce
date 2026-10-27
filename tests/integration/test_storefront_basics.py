"""Phase A: what a storefront can learn from the API, and what the API no longer shows or serves outside development."""

from decimal import Decimal

from app.core.config import Settings, get_settings

API = "/api/v1"
BASE = dict(database_url="postgresql+asyncpg://u:p@db/x", jwt_secret="x", stripe_secret_key="sk_test_x", stripe_webhook_secret="whsec_x")


async def test_public_store_settings_describe_how_buying_works(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "flat_shipping_cents", 4900)
    monkeypatch.setattr(s, "free_shipping_threshold_cents", 99900)
    monkeypatch.setattr(s, "tax_rate_percent", Decimal("18"))
    monkeypatch.setattr(s, "payment_currencies_enabled", "usd")
    body = (await client.get(f"{API}/store/settings")).json()  # no login needed
    assert body["currency"] == "usd" and body["minor_unit_exponent"] == 2
    assert body["flat_shipping_cents"] == 4900 and body["free_shipping_threshold_cents"] == 99900
    assert body["pending_order_timeout_minutes"] == 30
    assert body["checkout_available"] is True and body["payments_available"] is True and body["unavailable_reasons"] == []
    assert "tax_rate_percent" not in body and "tax" not in " ".join(body)  # the rate is never published


async def test_store_settings_say_why_checkout_or_payment_would_fail(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "flat_shipping_cents", None)
    monkeypatch.setattr(s, "tax_rate_percent", None)
    monkeypatch.setattr(s, "payment_currencies_enabled", "")
    body = (await client.get(f"{API}/store/settings")).json()
    assert body["checkout_available"] is False and body["payments_available"] is False
    assert set(body["unavailable_reasons"]) == {"Shipping is not configured", "Tax is not configured",
                                                "Payments are not enabled for the store currency"}
    assert body["free_shipping_threshold_cents"] is None or isinstance(body["free_shipping_threshold_cents"], int)
    # ... and that agrees with what checkout really does
    assert (await client.post(f"{API}/orders/checkout", json={"shipping_address": {
        "recipient_name": "R", "line1": "1 St", "city": "C", "state": "MH", "postal_code": "400001", "country": "IN"},
        "guest_email": "x@example.com"}, headers={"Idempotency-Key": "sf-1"})).status_code in (400, 409, 422, 503)


def test_api_docs_are_off_in_staging_and_production_unless_asked_for():
    on = {"docs_url": "/docs", "redoc_url": "/redoc", "openapi_url": "/openapi.json"}
    off = dict.fromkeys(on)
    assert Settings(_env_file=None, environment="development", **BASE).docs_urls() == on
    assert Settings(_env_file=None, environment="testing", **BASE).docs_urls() == on
    hardened = dict(BASE, jwt_secret="9f2c4a7e1b8d3c6a5e0f7b2d4c9a1e8f6b3d5c7a9e1f2b4d6c8a0e3f5b7d9c1a",
                    stripe_secret_key="sk_test_51Nabcdefghijklmnopqrstuv", stripe_webhook_secret="whsec_8d3f1a6b9c2e5d7f0a4b8c1e3d6f9a2b")
    for env in ("staging", "production"):
        assert Settings(_env_file=None, environment=env, **hardened).docs_urls() == off
        assert Settings(_env_file=None, environment=env, api_docs_enabled=True, **hardened).docs_urls() == on  # explicit override
    assert Settings(_env_file=None, environment="development", api_docs_enabled=False, **BASE).docs_urls() == off
