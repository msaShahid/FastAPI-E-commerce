# ruff: noqa: F811  (fixtures imported from the lifecycle suite are re-declared as test parameters)
"""M4: a customer never receives the provider's own error text; operators get it redacted."""

import io
import logging

from test_order_payment_lifecycle import API, category, make_order, one, paid_order, provider  # noqa: F401

from app.core.logging_config import build_handler
from app.core.payments.exceptions import PaymentProviderError, PaymentProviderTimeoutError

UPSTREAM = "Invalid API Key provided: sk_test_LEAKME0123456789abcdef. Customer jane.doe@example.com"


async def test_a_provider_failure_gives_the_customer_a_fixed_message_and_the_log_a_redacted_one(client, admin_headers, category, provider):
    h, o, p = await make_order(client, admin_headers, category)

    async def fail():
        raise PaymentProviderError(UPSTREAM)

    provider.on_create = fail
    stream = io.StringIO()
    handler = build_handler("json", stream)
    logging.getLogger().addHandler(handler)
    try:
        r = await client.post(f"{API}/payments", json={"order_id": o["id"]}, headers=h)
    finally:
        logging.getLogger().removeHandler(handler)

    assert r.status_code == 502
    body = r.text
    assert "Invalid API Key" not in body and "sk_test_" not in body and "jane.doe" not in body
    assert r.json()["detail"] == "The payment provider is unavailable. Please try again."
    assert r.json()["request_id"]  # the customer can quote it; the operator finds the log line with it
    logged = stream.getvalue()
    assert "Upstream service error" in logged and "Invalid API Key" in logged  # the operator still sees what happened ...
    assert "LEAKME" not in logged and "jane.doe" not in logged  # ... without the key or the customer's address


async def test_a_provider_timeout_has_its_own_fixed_message(client, admin_headers, category, provider):
    h, o, p = await make_order(client, admin_headers, category)

    async def slow():
        raise PaymentProviderTimeoutError(UPSTREAM)

    provider.on_create = slow
    r = await client.post(f"{API}/payments", json={"order_id": o["id"]}, headers=h)
    assert r.status_code == 502 and "LEAKME" not in r.text
    assert r.json()["detail"] == "The payment provider did not respond in time. Please try again."


async def test_an_upstream_message_stored_for_operators_is_redacted(
    client, admin_headers, category, provider, db_session, test_session_factory, monkeypatch
):
    import pytest

    from app.core.config import get_settings
    from app.modules.payments.jobs import execute_refund

    monkeypatch.setattr(get_settings(), "refund_max_attempts", 2)
    h, o, pay, pi = await paid_order(client, admin_headers, category, provider)
    rid = (await client.post(f"{API}/payments/{o['id']}/refund", headers=admin_headers)).json()["id"]

    async def refused(**kwargs):
        raise PaymentProviderTimeoutError(UPSTREAM)

    provider.create_refund = refused
    with pytest.raises(PaymentProviderError):
        await execute_refund({"refund_id": rid}, test_session_factory)  # attempt 1: will retry
    await execute_refund({"refund_id": rid}, test_session_factory)  # attempt 2: gives up and stores the reason

    stored = await one(db_session, "SELECT failure_reason FROM refunds")
    recon = await one(db_session, "SELECT string_agg(details::text, ' ') FROM payment_reconciliations WHERE kind = 'refund_uncertain'")
    assert "Invalid API Key" in stored and "Invalid API Key" in recon  # the operator still learns what happened ...
    assert "LEAKME" not in stored + recon and "jane.doe" not in stored + recon  # ... without the key or the address
