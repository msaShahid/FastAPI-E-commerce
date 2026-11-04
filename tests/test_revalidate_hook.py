"""The storefront cache hook: a correctly signed request, retries that never raise, and nothing sent when it is not configured."""

import hashlib
import hmac
import json

import httpx
import pytest
from pydantic import SecretStr

from app.core import revalidate
from app.core.config import ConfigurationError, Settings, get_settings

SECRET = "s" * 40


@pytest.fixture
def configured(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "frontend_revalidate_url", "http://web:3000/api/bff/revalidate")
    monkeypatch.setattr(s, "frontend_revalidate_secret", SecretStr(SECRET))


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_the_request_is_signed_exactly_as_the_storefront_verifies_it(configured):
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "revalidated": 2})

    assert await revalidate.notify_frontend(["product:12", "catalogue", "catalogue"], client=client_for(handler)) is True
    (request,) = seen
    body = request.content.decode()
    assert json.loads(body) == {"tags": ["catalogue", "product:12"]}  # sorted, de-duplicated
    timestamp = request.headers["x-nc-timestamp"]
    expected = hmac.new(SECRET.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()  # the documented formula, written out again
    assert request.headers["x-nc-signature"] == expected and request.headers["content-type"] == "application/json"
    assert SECRET not in body and SECRET not in str(request.headers)


async def test_a_failure_is_retried_then_succeeds(configured):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json={"ok": True})

    assert await revalidate.notify_frontend(["catalogue"], client=client_for(handler), delays=(0, 0, 0)) is True
    assert len(calls) == 3


async def test_it_gives_up_without_raising_and_a_refusal_is_not_retried(configured):
    calls = []

    def down(request):
        calls.append(1)
        raise httpx.ConnectError("storefront is down")

    assert await revalidate.notify_frontend(["catalogue"], client=client_for(down), delays=(0, 0)) is False
    assert len(calls) == 3  # one try and two retries

    refused = []
    assert await revalidate.notify_frontend(["catalogue"], client=client_for(lambda r: refused.append(1) or httpx.Response(401)), delays=(0, 0)) is False
    assert len(refused) == 1  # a bad secret is not fixed by trying again


async def test_nothing_is_sent_when_it_is_not_configured_or_there_is_nothing_valid_to_send(monkeypatch):
    sent = []
    client = client_for(lambda r: sent.append(r) or httpx.Response(200))
    monkeypatch.setattr(get_settings(), "frontend_revalidate_url", None)
    assert await revalidate.notify_frontend(["catalogue"], client=client) is False
    monkeypatch.setattr(get_settings(), "frontend_revalidate_url", "http://web/x")
    monkeypatch.setattr(get_settings(), "frontend_revalidate_secret", SecretStr(SECRET))
    assert await revalidate.notify_frontend(["not a tag", "product:abc", "../etc"], client=client) is False
    assert sent == []


def test_only_tags_the_storefront_accepts_are_sent_and_the_cap_keeps_the_broad_ones():
    assert revalidate.clean_tags(["catalogue", "product:1", "category:2", "store-settings", "site-config", "x", "product:-1", 5]) == [
        "catalogue", "category:2", "product:1", "site-config", "store-settings"]
    many = [f"product:{i}" for i in range(80)] + ["catalogue"]
    assert revalidate.clean_tags(many) == ["catalogue"]


def test_the_url_and_secret_must_be_set_together_and_the_secret_must_be_long(clean_settings_env):
    base = dict(database_url="postgresql+asyncpg://u:p@h/d", jwt_secret="j" * 40, stripe_secret_key="sk_test_x", stripe_webhook_secret="whsec_x")
    with pytest.raises(Exception, match="set together"):
        Settings(**base, frontend_revalidate_url="http://web/x")
    with pytest.raises(Exception, match="32 characters"):
        Settings(**base, frontend_revalidate_url="http://web/x", frontend_revalidate_secret="short")
    assert Settings(**base, frontend_revalidate_url="http://web/x", frontend_revalidate_secret=SECRET).frontend_revalidate_url
    assert ConfigurationError  # imported to document which error type the settings raise
