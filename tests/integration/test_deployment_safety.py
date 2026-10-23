"""M1: the guest cookies are Secure in every hardened environment (production AND staging), not only one label."""

import pytest

from app.core.config import get_settings

API = "/api/v1"


@pytest.mark.parametrize("environment,secure", [("production", True), ("staging", True), ("development", False), ("testing", False)])
async def test_guest_cart_cookie_secure_flag_follows_the_hardened_environments(client, monkeypatch, environment, secure):
    monkeypatch.setattr(get_settings(), "environment", environment)
    client.cookies.clear()
    r = await client.get(f"{API}/cart")
    header = next(h for h in r.headers.get_list("set-cookie") if h.startswith("guest_cart_token"))
    assert ("Secure" in header) is secure
    assert "HttpOnly" in header
