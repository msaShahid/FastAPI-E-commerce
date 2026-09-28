"""
Proves the webhook idempotency claim is safe under REAL concurrent
delivery -- two independent DB connections processing the same event_id
at the same instant -- not just sequential re-delivery (already covered
by the fake-repository unit tests in tests/payments/).

Stripe's at-least-once delivery guarantee means this can genuinely
happen: two workers, two connections, same webhook payload, arriving
close enough together to overlap in time.
"""

import asyncio
import uuid

from sqlalchemy import select

from app.core.payments.interface import WebhookEvent
from app.modules.auth.models.user import User
from app.modules.cart.repositories.cart_repository import CartRepository
from app.modules.categories.models.category import Category
from app.modules.orders.models.order import Order
from app.modules.orders.models.order_status_history import OrderStatusHistory
from app.modules.orders.repositories.order_repository import OrderRepository
from app.modules.orders.services.order_service import OrderService
from app.modules.payments.models.payment import Payment
from app.modules.payments.repositories.payment_repository import PaymentRepository
from app.modules.payments.services.payment_service import SUCCEEDED_EVENT, PaymentService
from app.modules.products.models.product import Product
from app.modules.products.repositories.product_repository import ProductRepository
from app.shared.enums.order_status import OrderStatus
from app.shared.enums.payment_status import PaymentStatus
from app.shared.enums.product_status import ProductStatus
from app.shared.enums.roles import UserRole


async def _seed_pending_order_with_payment(db_session):
    category = Category(name="Webhook Race", slug="webhook-race")
    db_session.add(category)
    await db_session.flush()

    product = Product(
        name="Race Widget",
        slug="race-widget",
        price_cents=1000,
        sku="RACE-001",
        stock=5,
        category_id=category.id,
        status=ProductStatus.ACTIVE,
    )
    db_session.add(product)
    await db_session.flush()

    user = User(
        id=uuid.uuid4(),
        username="webhook_racer",
        email="webhook_racer@example.com",
        password_hash="irrelevant-for-this-test",
        role=UserRole.USER,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()

    order = Order(
        id=uuid.uuid4(),
        user_id=user.id,
        idempotency_key="webhook-race-key",
        status=OrderStatus.PENDING,
        subtotal_cents=1000,
        shipping_cents=0,
        tax_cents=0,
        total_cents=1000,
    )
    db_session.add(order)
    await db_session.flush()

    payment = Payment(
        id=uuid.uuid4(),
        order_id=order.id,
        provider="stripe",
        provider_payment_id="pi_webhook_race",
        amount_cents=1000,
        currency="usd",
        status=PaymentStatus.PENDING,
    )
    db_session.add(payment)
    await db_session.flush()

    await db_session.commit()
    return order, payment


async def _deliver_webhook(session_factory, event: WebhookEvent):
    """
    One complete webhook delivery in its OWN session/transaction --
    mirroring exactly what a real request does: commit on success,
    rollback on failure. No real PaymentProvider is needed here --
    process_webhook_event never calls it, only handle_webhook does.
    """
    async with session_factory() as session:
        payment_service = PaymentService(
            PaymentRepository(session),
            OrderService(
                OrderRepository(session),
                CartRepository(session),
                ProductRepository(session),
            ),
            provider=None,
        )
        try:
            await payment_service.process_webhook_event(event)
            await session.commit()
            return "done"
        except Exception as exc:
            await session.rollback()
            return f"error: {type(exc).__name__}: {exc}"


async def test_concurrent_duplicate_webhook_delivery_applies_side_effects_once(
    db_session, test_session_factory
):
    """
    THE test. Same event_id, same instant, two separate connections.

    Correct behavior: the order transitions pending -> paid exactly
    ONCE (one history row), even though the webhook "arrived" twice at
    the same moment. Claiming the event_id inside the same atomic
    INSERT ... ON CONFLICT (rather than checking for it, running the
    handler, then recording it afterwards) is what closes this race --
    without it, both deliveries could pass the "have I seen this?"
    check before either has recorded it.
    """
    order, payment = await _seed_pending_order_with_payment(db_session)

    event = WebhookEvent(
        event_id="evt_concurrent_dup",
        event_type=SUCCEEDED_EVENT,
        provider_payment_id=payment.provider_payment_id,
    )

    results = await asyncio.gather(
        _deliver_webhook(test_session_factory, event),
        _deliver_webhook(test_session_factory, event),
    )

    assert results == ["done", "done"], (
        f"Both deliveries should complete without error (the loser just "
        f"no-ops), got: {results}"
    )

    async with test_session_factory() as verify_session:
        history_result = await verify_session.execute(
            select(OrderStatusHistory).where(
                OrderStatusHistory.order_id == order.id,
                OrderStatusHistory.to_status == OrderStatus.PAID,
            )
        )
        paid_transitions = history_result.scalars().all()
        assert len(paid_transitions) == 1, (
            f"Expected exactly one pending->paid transition, got "
            f"{len(paid_transitions)} -- the claim-then-process race "
            f"was not actually closed."
        )

        order_result = await verify_session.execute(
            select(Order).where(Order.id == order.id)
        )
        assert order_result.scalar_one().status == OrderStatus.PAID

        payment_result = await verify_session.execute(
            select(Payment).where(Payment.id == payment.id)
        )
        assert payment_result.scalar_one().status == PaymentStatus.SUCCEEDED
