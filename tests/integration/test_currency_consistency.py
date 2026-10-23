# ruff: noqa: F811  (fixtures imported from the lifecycle suite are re-declared as test parameters)
"""Phase 7: money is described in the order's own currency, and nothing silently defaults to USD."""

import uuid

import pytest
from _store_currency import reset_store_currency
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from test_order_payment_lifecycle import category, make_order, one, provider  # noqa: F401

from app.modules.payments import jobs as payment_jobs
from app.modules.payments.repositories.payment_repository import PaymentRepository


class Outbox:
    def __init__(self):
        self.sent = []

    async def send(self, *, to, subject, body):
        self.sent.append(body)


async def confirmation_body(monkeypatch, test_session_factory, order_id):
    outbox = Outbox()
    monkeypatch.setattr(payment_jobs, "get_email_sender", lambda: outbox)
    await payment_jobs.send_order_confirmation({"order_id": order_id}, test_session_factory)
    return outbox.sent[0]


@pytest.mark.parametrize(("code", "expected"), [("usd", "Total paid: 10.00 USD"), ("inr", "Total paid: 10.00 INR"), ("jpy", "Total paid: 1000 JPY")])
async def test_the_confirmation_email_uses_the_orders_currency_and_exponent(
    client, admin_headers, category, provider, test_session_factory, monkeypatch, code, expected
):
    await reset_store_currency(code)
    h, o, p = await make_order(client, admin_headers, category)
    assert o["currency"] == code
    assert expected in await confirmation_body(monkeypatch, test_session_factory, o["id"])  # the old `/ 100` printed 10.00 for yen


async def test_the_confirmation_email_never_guesses_a_currency_for_a_legacy_order(
    client, admin_headers, category, provider, db_session, test_session_factory, monkeypatch
):
    h, o, p = await make_order(client, admin_headers, category)
    await db_session.execute(text("ALTER TABLE orders DISABLE TRIGGER USER"))
    await db_session.execute(text("UPDATE orders SET currency = NULL WHERE id = :o"), {"o": o["id"]})  # a legacy order
    await db_session.execute(text("ALTER TABLE orders ENABLE TRIGGER USER"))
    await db_session.commit()
    body = await confirmation_body(monkeypatch, test_session_factory, o["id"])
    assert "Total paid" not in body and "Order " in body and "10.00" not in body


async def test_a_payment_must_state_its_currency_it_never_becomes_usd(db_session):
    with pytest.raises(TypeError):
        await PaymentRepository(db_session).create(order_id=uuid.uuid4(), provider_payment_id="pi_x", amount_cents=1)  # type: ignore[call-arg]


async def test_the_payments_table_has_no_currency_default(client, admin_headers, category, provider, db_session):
    h, o, p = await make_order(client, admin_headers, category)
    with pytest.raises(IntegrityError):  # NOT NULL with no default: a forgotten currency fails loudly
        await db_session.execute(text(
            "INSERT INTO payments (id, order_id, provider, provider_payment_id, amount_cents, status) "
            "VALUES (gen_random_uuid(), :o, 'stripe', 'pi_nodefault', 1000, 'pending')"), {"o": o["id"]})
    await db_session.rollback()
