import json
from uuid import uuid4

import pytest

from app.core.exceptions import BadRequestError
from app.core.payments.exceptions import InvalidWebhookSignatureError
from app.modules.orders.exceptions.order_exceptions import (
    OrderAccessForbiddenError,
    OrderNotFoundError,
)
from app.modules.orders.models.order import Order
from app.modules.payments.exceptions.payment_exceptions import (
    OrderNotPayableError,
    OrderNotRefundableError,
)
from app.shared.enums.order_status import OrderStatus
from app.shared.enums.payment_status import PaymentStatus

SUCCEEDED_EVENT = "payment_intent.succeeded"
FAILED_EVENT = "payment_intent.payment_failed"


def make_order(repo, *, user_id, status, total_cents=5000) -> Order:
    order = Order(
        id=uuid4(),
        user_id=user_id,
        idempotency_key=f"key-{uuid4()}",
        status=status,
        subtotal_cents=total_cents,
        shipping_cents=0,
        tax_cents=0,
        total_cents=total_cents,
    )
    order.items = []
    repo.orders[order.id] = order
    return order


def make_webhook_payload(
    *, event_id: str, event_type: str, provider_payment_id: str
) -> bytes:
    return json.dumps(
        {
            "event_id": event_id,
            "event_type": event_type,
            "provider_payment_id": provider_payment_id,
        }
    ).encode()


# --- create_payment_for_order: happy path ---


async def test_create_payment_creates_new_intent(
    payment_service, fake_order_repository_for_payments, fake_payment_provider
):
    user_id = uuid4()
    order = make_order(
        fake_order_repository_for_payments,
        user_id=user_id,
        status=OrderStatus.PENDING,
        total_cents=5000,
    )

    payment, client_secret = await payment_service.create_payment_for_order(
        order_id=order.id, user_id=user_id
    )

    assert payment.order_id == order.id
    assert payment.status == PaymentStatus.PENDING
    assert payment.amount_cents == 5000
    assert (
        client_secret
        == fake_payment_provider.intents[payment.provider_payment_id]["client_secret"]
    )
    assert len(fake_payment_provider.intents) == 1


async def test_create_payment_rejects_non_owner(
    payment_service, fake_order_repository_for_payments
):
    owner_id = uuid4()
    other_user_id = uuid4()
    order = make_order(
        fake_order_repository_for_payments, user_id=owner_id, status=OrderStatus.PENDING
    )

    with pytest.raises(OrderAccessForbiddenError):
        await payment_service.create_payment_for_order(
            order_id=order.id, user_id=other_user_id
        )


async def test_create_payment_rejects_non_pending_order(
    payment_service, fake_order_repository_for_payments
):
    user_id = uuid4()
    order = make_order(
        fake_order_repository_for_payments,
        user_id=user_id,
        status=OrderStatus.CANCELLED,
    )

    with pytest.raises(OrderNotPayableError):
        await payment_service.create_payment_for_order(
            order_id=order.id, user_id=user_id
        )


async def test_create_payment_rejects_already_paid_order(
    payment_service, fake_order_repository_for_payments, fake_payment_repository
):
    user_id = uuid4()
    order = make_order(
        fake_order_repository_for_payments, user_id=user_id, status=OrderStatus.PENDING
    )
    await fake_payment_repository.create(
        order_id=order.id, provider_payment_id="pi_already_paid", amount_cents=5000
    )
    existing = await fake_payment_repository.get_by_provider_payment_id(
        "pi_already_paid"
    )
    await fake_payment_repository.update_status(existing, PaymentStatus.SUCCEEDED)

    with pytest.raises(OrderNotPayableError):
        await payment_service.create_payment_for_order(
            order_id=order.id, user_id=user_id
        )


# --- create_payment_for_order: reuse / retry logic ---


async def test_create_payment_reuses_pending_payment(
    payment_service, fake_order_repository_for_payments, fake_payment_provider
):
    user_id = uuid4()
    order = make_order(
        fake_order_repository_for_payments, user_id=user_id, status=OrderStatus.PENDING
    )

    first_payment, first_secret = await payment_service.create_payment_for_order(
        order_id=order.id, user_id=user_id
    )
    second_payment, second_secret = await payment_service.create_payment_for_order(
        order_id=order.id, user_id=user_id
    )

    assert first_payment.id == second_payment.id
    assert first_secret == second_secret
    assert (
        len(fake_payment_provider.intents) == 1
    )  # only one Stripe intent ever created


async def test_create_payment_reuses_still_open_intent_after_local_failure(
    payment_service,
    fake_order_repository_for_payments,
    fake_payment_repository,
    fake_payment_provider,
):
    """
    A payment we marked FAILED (e.g. a card decline) can still be retried
    by the customer if Stripe's intent is still open. We must not create
    a second, competing intent in that case.
    """
    user_id = uuid4()
    order = make_order(
        fake_order_repository_for_payments, user_id=user_id, status=OrderStatus.PENDING
    )

    payment, _ = await payment_service.create_payment_for_order(
        order_id=order.id, user_id=user_id
    )
    await fake_payment_repository.update_status(payment, PaymentStatus.FAILED)
    # Stripe's intent is still retriable (e.g. customer can try another card).
    fake_payment_provider.set_intent_status(
        payment.provider_payment_id, "requires_payment_method"
    )

    reused_payment, _ = await payment_service.create_payment_for_order(
        order_id=order.id, user_id=user_id
    )

    assert reused_payment.id == payment.id
    assert reused_payment.status == PaymentStatus.PENDING
    assert len(fake_payment_provider.intents) == 1


async def test_create_payment_makes_new_intent_after_stripe_intent_canceled(
    payment_service,
    fake_order_repository_for_payments,
    fake_payment_repository,
    fake_payment_provider,
):
    user_id = uuid4()
    order = make_order(
        fake_order_repository_for_payments, user_id=user_id, status=OrderStatus.PENDING
    )

    payment, _ = await payment_service.create_payment_for_order(
        order_id=order.id, user_id=user_id
    )
    await fake_payment_repository.update_status(payment, PaymentStatus.FAILED)
    fake_payment_provider.set_intent_status(payment.provider_payment_id, "canceled")

    new_payment, _ = await payment_service.create_payment_for_order(
        order_id=order.id, user_id=user_id
    )

    assert new_payment.id != payment.id
    assert new_payment.provider_payment_id != payment.provider_payment_id
    assert len(fake_payment_provider.intents) == 2


async def test_create_payment_syncs_succeeded_intent_missed_by_webhook(
    payment_service,
    fake_order_repository_for_payments,
    fake_payment_repository,
    fake_payment_provider,
):
    """If Stripe says the existing intent already succeeded but our webhook
    never arrived, we must not create a second intent -- sync and refuse."""
    user_id = uuid4()
    order = make_order(
        fake_order_repository_for_payments, user_id=user_id, status=OrderStatus.PENDING
    )

    payment, _ = await payment_service.create_payment_for_order(
        order_id=order.id, user_id=user_id
    )
    fake_payment_provider.set_intent_status(payment.provider_payment_id, "succeeded")

    with pytest.raises(OrderNotPayableError):
        await payment_service.create_payment_for_order(
            order_id=order.id, user_id=user_id
        )

    assert payment.status == PaymentStatus.SUCCEEDED  # synced locally as a side effect
    assert len(fake_payment_provider.intents) == 1  # no second intent was created


# --- webhook handling ---


async def test_handle_webhook_invalid_signature_is_a_bad_request(payment_service):
    exc_info = None
    try:
        await payment_service.handle_webhook(
            payload=b"{}", signature_header="wrong-signature"
        )
    except InvalidWebhookSignatureError as exc:
        exc_info = exc

    assert exc_info is not None
    assert isinstance(
        exc_info, BadRequestError
    )  # maps to HTTP 400 via the global handler


async def test_webhook_success_transitions_order_to_paid(
    payment_service, fake_order_repository_for_payments, fake_payment_repository
):
    user_id = uuid4()
    order = make_order(
        fake_order_repository_for_payments, user_id=user_id, status=OrderStatus.PENDING
    )
    payment, _ = await payment_service.create_payment_for_order(
        order_id=order.id, user_id=user_id
    )

    payload = make_webhook_payload(
        event_id="evt_1",
        event_type=SUCCEEDED_EVENT,
        provider_payment_id=payment.provider_payment_id,
    )
    await payment_service.handle_webhook(
        payload=payload, signature_header="valid-signature"
    )

    assert payment.status == PaymentStatus.SUCCEEDED
    assert order.status == OrderStatus.PAID


async def test_webhook_duplicate_event_is_processed_once(
    payment_service, fake_order_repository_for_payments, fake_payment_repository
):
    user_id = uuid4()
    order = make_order(
        fake_order_repository_for_payments, user_id=user_id, status=OrderStatus.PENDING
    )
    payment, _ = await payment_service.create_payment_for_order(
        order_id=order.id, user_id=user_id
    )

    payload = make_webhook_payload(
        event_id="evt_dup",
        event_type=SUCCEEDED_EVENT,
        provider_payment_id=payment.provider_payment_id,
    )
    await payment_service.handle_webhook(
        payload=payload, signature_header="valid-signature"
    )
    await payment_service.handle_webhook(
        payload=payload, signature_header="valid-signature"
    )

    # Only ONE order status transition should have been recorded, even
    # though the webhook "arrived" twice.
    assert len(fake_order_repository_for_payments.status_changes) == 1
    assert order.status == OrderStatus.PAID


async def test_webhook_failed_marks_payment_failed_without_touching_order(
    payment_service, fake_order_repository_for_payments, fake_payment_repository
):
    user_id = uuid4()
    order = make_order(
        fake_order_repository_for_payments, user_id=user_id, status=OrderStatus.PENDING
    )
    payment, _ = await payment_service.create_payment_for_order(
        order_id=order.id, user_id=user_id
    )

    payload = make_webhook_payload(
        event_id="evt_fail",
        event_type=FAILED_EVENT,
        provider_payment_id=payment.provider_payment_id,
    )
    await payment_service.handle_webhook(
        payload=payload, signature_header="valid-signature"
    )

    assert payment.status == PaymentStatus.FAILED
    assert order.status == OrderStatus.PENDING
    assert fake_order_repository_for_payments.status_changes == []


async def test_webhook_success_for_non_pending_order_does_not_transition(
    payment_service, fake_order_repository_for_payments, fake_payment_repository
):
    """
    Money was taken (Stripe confirms succeeded) but the order is no
    longer PENDING (e.g. it was cancelled out from under the payment).
    The payment is still marked SUCCEEDED locally (so it shows up for
    reconciliation), but the order must NOT silently become PAID.
    """
    user_id = uuid4()
    order = make_order(
        fake_order_repository_for_payments, user_id=user_id, status=OrderStatus.PENDING
    )
    payment, _ = await payment_service.create_payment_for_order(
        order_id=order.id, user_id=user_id
    )
    order.status = OrderStatus.CANCELLED  # e.g. an admin cancelled it in the meantime

    payload = make_webhook_payload(
        event_id="evt_2",
        event_type=SUCCEEDED_EVENT,
        provider_payment_id=payment.provider_payment_id,
    )
    await payment_service.handle_webhook(
        payload=payload, signature_header="valid-signature"
    )

    assert payment.status == PaymentStatus.SUCCEEDED
    assert order.status == OrderStatus.CANCELLED
    assert fake_order_repository_for_payments.status_changes == []


# --- refund_payment ---


async def test_refund_payment_happy_path(
    payment_service,
    fake_order_repository_for_payments,
    fake_payment_repository,
    fake_payment_provider,
):
    user_id = uuid4()
    admin_id = uuid4()
    order = make_order(
        fake_order_repository_for_payments, user_id=user_id, status=OrderStatus.PAID
    )
    payment = await fake_payment_repository.create(
        order_id=order.id, provider_payment_id="pi_paid", amount_cents=5000
    )
    await fake_payment_repository.update_status(payment, PaymentStatus.SUCCEEDED)

    refunded_payment, updated_order = await payment_service.refund_payment(
        order_id=order.id, admin_user_id=admin_id
    )

    assert refunded_payment.status == PaymentStatus.REFUNDED
    assert refunded_payment.refunded_amount_cents == 5000
    assert refunded_payment.provider_refund_id in fake_payment_provider.refunds
    assert updated_order.status == OrderStatus.REFUNDED
    assert fake_order_repository_for_payments.status_changes == [
        (OrderStatus.PAID, OrderStatus.REFUNDED, admin_id)
    ]


async def test_refund_payment_allows_fulfilled_order(
    payment_service, fake_order_repository_for_payments, fake_payment_repository
):
    user_id = uuid4()
    order = make_order(
        fake_order_repository_for_payments,
        user_id=user_id,
        status=OrderStatus.FULFILLED,
    )
    payment = await fake_payment_repository.create(
        order_id=order.id, provider_payment_id="pi_fulfilled", amount_cents=5000
    )
    await fake_payment_repository.update_status(payment, PaymentStatus.SUCCEEDED)

    _, updated_order = await payment_service.refund_payment(
        order_id=order.id, admin_user_id=uuid4()
    )

    assert updated_order.status == OrderStatus.REFUNDED


async def test_refund_payment_rejects_pending_order(
    payment_service, fake_order_repository_for_payments
):
    order = make_order(
        fake_order_repository_for_payments, user_id=uuid4(), status=OrderStatus.PENDING
    )

    with pytest.raises(OrderNotRefundableError):
        await payment_service.refund_payment(order_id=order.id, admin_user_id=uuid4())


async def test_refund_payment_rejects_already_refunded_order(
    payment_service, fake_order_repository_for_payments
):
    order = make_order(
        fake_order_repository_for_payments, user_id=uuid4(), status=OrderStatus.REFUNDED
    )

    with pytest.raises(OrderNotRefundableError):
        await payment_service.refund_payment(order_id=order.id, admin_user_id=uuid4())


async def test_refund_payment_rejects_paid_order_with_no_succeeded_payment(
    payment_service, fake_order_repository_for_payments, fake_payment_repository
):
    """
    Shouldn't normally happen (an order only reaches PAID via a succeeded
    payment webhook), but must fail loudly rather than refund nothing.
    """
    order = make_order(
        fake_order_repository_for_payments, user_id=uuid4(), status=OrderStatus.PAID
    )
    payment = await fake_payment_repository.create(
        order_id=order.id, provider_payment_id="pi_not_succeeded", amount_cents=5000
    )
    await fake_payment_repository.update_status(payment, PaymentStatus.FAILED)

    with pytest.raises(OrderNotRefundableError):
        await payment_service.refund_payment(order_id=order.id, admin_user_id=uuid4())


async def test_refund_payment_missing_order_raises_not_found(payment_service):
    with pytest.raises(OrderNotFoundError):
        await payment_service.refund_payment(order_id=uuid4(), admin_user_id=uuid4())


async def test_refund_payment_is_retry_safe(
    payment_service,
    fake_order_repository_for_payments,
    fake_payment_repository,
    fake_payment_provider,
):
    """
    A retried refund request for the same payment must hit the same
    Stripe refund (via the idempotency key), not create a second one.
    """
    order = make_order(
        fake_order_repository_for_payments, user_id=uuid4(), status=OrderStatus.PAID
    )
    payment = await fake_payment_repository.create(
        order_id=order.id, provider_payment_id="pi_retry", amount_cents=5000
    )
    await fake_payment_repository.update_status(payment, PaymentStatus.SUCCEEDED)

    admin_id = uuid4()
    await payment_service.refund_payment(order_id=order.id, admin_user_id=admin_id)

    # Order is REFUNDED now, so a second attempt is correctly rejected at
    # the status gate before it would ever reach Stripe again.
    with pytest.raises(OrderNotRefundableError):
        await payment_service.refund_payment(order_id=order.id, admin_user_id=admin_id)

    assert len(fake_payment_provider.refunds) == 1
