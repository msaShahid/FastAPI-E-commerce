# ruff: noqa: F811  (fixtures imported from the lifecycle suite are re-declared as test parameters)
"""M6: the money-moving routes are rate limited per client address; a Redis outage degrades instead of failing; proxy trust is flagged."""

import logging
import uuid

from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from test_order_payment_lifecycle import ADDR, API, category, make_order, provider  # noqa: F401

from app.core.config import get_settings
from app.core.rate_limit import build_limiter, limiter, warn_if_forwarded_headers_untrusted
from app.main import app

CHECKOUT = {"shipping_address": ADDR, "guest_email": "limit@example.com"}


async def statuses(call, n):
    return [(await call()).status_code for _ in range(n)]


async def test_checkout_is_limited_per_client_and_the_limit_is_configurable(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "rate_limit_checkout", "2/minute")
    limiter.reset()
    codes = await statuses(lambda: client.post(f"{API}/orders/checkout", json=CHECKOUT, headers={"Idempotency-Key": uuid.uuid4().hex}), 4)
    assert codes[2:] == [429, 429] and 429 not in codes[:2]  # the first two reach checkout (and fail on the empty cart), the rest are refused


async def test_payment_creation_is_limited(client, admin_headers, monkeypatch):
    monkeypatch.setattr(get_settings(), "rate_limit_payment", "2/minute")
    limiter.reset()
    codes = await statuses(lambda: client.post(f"{API}/payments", json={"order_id": str(uuid.uuid4())}, headers=admin_headers), 3)
    assert codes[2] == 429 and 429 not in codes[:2]


async def test_cart_mutation_is_limited(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "rate_limit_cart", "2/minute")
    limiter.reset()
    codes = await statuses(lambda: client.post(f"{API}/cart/items", json={"product_id": 999999, "quantity": 1}), 3)
    assert codes[2] == 429 and 429 not in codes[:2]


async def test_one_noisy_client_does_not_lock_out_another(client, monkeypatch):
    """Limits are per client address. (Behind a proxy that address is the PROXY's unless FORWARDED_ALLOW_IPS names it: see below.)"""
    monkeypatch.setattr(get_settings(), "rate_limit_cart", "2/minute")
    limiter.reset()
    body = {"product_id": 999999, "quantity": 1}
    async with AsyncClient(transport=ASGITransport(app=app, client=("10.0.0.1", 1111)), base_url="http://test") as noisy, \
            AsyncClient(transport=ASGITransport(app=app, client=("10.0.0.2", 2222)), base_url="http://test") as quiet:
        assert (await statuses(lambda: noisy.post(f"{API}/cart/items", json=body), 3))[2] == 429
        assert (await quiet.post(f"{API}/cart/items", json=body)).status_code != 429


async def test_clients_sharing_one_address_share_one_bucket(client, monkeypatch):
    """What an unconfigured proxy does: every client arrives from the proxy's address."""
    monkeypatch.setattr(get_settings(), "rate_limit_cart", "2/minute")
    limiter.reset()
    body = {"product_id": 999999, "quantity": 1}
    same = ASGITransport(app=app, client=("10.9.9.9", 1))
    async with AsyncClient(transport=same, base_url="http://test") as a, AsyncClient(transport=same, base_url="http://test") as b:
        await a.post(f"{API}/cart/items", json=body)
        await b.post(f"{API}/cart/items", json=body)
        assert (await a.post(f"{API}/cart/items", json=body)).status_code == 429


async def test_a_redis_outage_degrades_to_an_in_memory_limit_instead_of_failing_requests():
    lim = build_limiter("redis://127.0.0.1:1/0", "3/minute")  # nothing listens there
    mini = FastAPI()
    mini.state.limiter = lim
    mini.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    @mini.get("/ping")
    @lim.limit("1000/minute")
    async def ping(request: Request):
        return {"ok": True}

    async with AsyncClient(transport=ASGITransport(app=mini), base_url="http://t") as c:
        codes = [(await c.get("/ping")).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]  # no 500s; the fallback limit still applies


def test_untrusted_forwarded_headers_in_a_hardened_environment_are_flagged(monkeypatch, caplog):
    monkeypatch.setattr(get_settings(), "environment", "production")
    with caplog.at_level(logging.WARNING, logger="app.core.rate_limit"):
        assert warn_if_forwarded_headers_untrusted({}) is True
    assert "FORWARDED_ALLOW_IPS" in caplog.text
    assert warn_if_forwarded_headers_untrusted({"FORWARDED_ALLOW_IPS": "10.0.0.5"}) is False
    monkeypatch.setattr(get_settings(), "environment", "development")
    assert warn_if_forwarded_headers_untrusted({}) is False
