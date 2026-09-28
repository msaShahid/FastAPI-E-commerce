import json
from uuid import UUID, uuid4

import pytest

from app.core.payments.exceptions import InvalidWebhookSignatureError
from app.core.payments.interface import (
    PaymentIntentResult,
    PaymentProvider,
    RefundResult,
    WebhookEvent,
)
from app.modules.orders.models.order import Order
from app.modules.orders.services.order_service import OrderService
from app.modules.payments.models.payment import Payment
from app.modules.payments.services.payment_service import PaymentService
from app.shared.enums.payment_status import PaymentStatus


class FakeOrderRepositoryForPayments:
    """
    Minimal stand-in for OrderRepository -- only the methods PaymentService
    actually goes through OrderService for (get_by_id, get_by_id_for_update,
    update_status). No real locking: these tests are single-threaded, so
    get_by_id_for_update is just get_by_id. The concurrency guarantee
    itself is covered separately at the integration level, the same way
    checkout's locking is (see test_checkout_concurrency.py).
    """

    def __init__(self) -> None:
        self.orders: dict[UUID, Order] = {}
        self.status_changes: list[tuple] = []

    async def get_by_id(self, order_id: UUID) -> Order | None:
        return self.orders.get(order_id)

    async def get_by_id_for_update(self, order_id: UUID) -> Order | None:
        return self.orders.get(order_id)

    async def update_status(
        self, order: Order, new_status, changed_by_user_id
    ) -> Order:
        self.status_changes.append((order.status, new_status, changed_by_user_id))
        order.status = new_status
        return order


class FakePaymentRepository:
    def __init__(self) -> None:
        self.payments: dict[UUID, Payment] = {}
        self.claimed_events: set[str] = set()

    async def get_by_id(self, payment_id: UUID) -> Payment | None:
        return self.payments.get(payment_id)

    async def get_by_provider_payment_id(
        self, provider_payment_id: str
    ) -> Payment | None:
        return next(
            (
                p
                for p in self.payments.values()
                if p.provider_payment_id == provider_payment_id
            ),
            None,
        )

    async def list_for_order(self, order_id: UUID) -> list[Payment]:
        return [p for p in self.payments.values() if p.order_id == order_id]

    async def create(
        self,
        *,
        order_id: UUID,
        provider_payment_id: str,
        amount_cents: int,
        currency: str = "usd",
        provider: str = "stripe",
    ) -> Payment:
        payment = Payment(
            id=uuid4(),
            order_id=order_id,
            provider=provider,
            provider_payment_id=provider_payment_id,
            amount_cents=amount_cents,
            currency=currency,
            status=PaymentStatus.PENDING,
        )
        self.payments[payment.id] = payment
        return payment

    async def update_status(self, payment: Payment, status: PaymentStatus) -> Payment:
        payment.status = status
        return payment

    async def mark_refunded(
        self, payment: Payment, *, provider_refund_id: str, refunded_amount_cents: int
    ) -> Payment:
        payment.status = PaymentStatus.REFUNDED
        payment.provider_refund_id = provider_refund_id
        payment.refunded_amount_cents = refunded_amount_cents
        return payment

    async def claim_webhook_event(self, event_id: str) -> bool:
        """Emulates INSERT ... ON CONFLICT DO NOTHING: True only the first time."""
        if event_id in self.claimed_events:
            return False
        self.claimed_events.add(event_id)
        return True


class FakePaymentProvider(PaymentProvider):
    """
    Records intents in memory and honors idempotency keys the same way
    Stripe does: calling create_payment_intent twice with the same key
    returns the SAME intent instead of creating a second one.
    """

    def __init__(self) -> None:
        self._next_id = 1
        self._next_refund_id = 1
        self.intents: dict[str, dict] = {}
        self._by_idempotency_key: dict[str, str] = {}
        self._refunds_by_idempotency_key: dict[str, str] = {}
        self.refunds: dict[str, dict] = {}
        self.valid_signature = "valid-signature"

    async def create_payment_intent(
        self, *, amount_cents: int, currency: str, metadata: dict, idempotency_key: str
    ) -> PaymentIntentResult:
        existing_id = self._by_idempotency_key.get(idempotency_key)
        if existing_id is not None:
            data = self.intents[existing_id]
            return PaymentIntentResult(
                provider_payment_id=existing_id,
                client_secret=data["client_secret"],
                status=data["status"],
            )

        provider_payment_id = f"pi_fake_{self._next_id}"
        self._next_id += 1
        client_secret = f"{provider_payment_id}_secret"
        self.intents[provider_payment_id] = {
            "status": "requires_payment_method",
            "client_secret": client_secret,
        }
        self._by_idempotency_key[idempotency_key] = provider_payment_id
        return PaymentIntentResult(
            provider_payment_id=provider_payment_id,
            client_secret=client_secret,
            status="requires_payment_method",
        )

    async def retrieve_payment_intent(
        self, *, provider_payment_id: str
    ) -> PaymentIntentResult:
        data = self.intents[provider_payment_id]
        return PaymentIntentResult(
            provider_payment_id=provider_payment_id,
            client_secret=data["client_secret"],
            status=data["status"],
        )

    async def create_refund(
        self, *, provider_payment_id: str, amount_cents: int, idempotency_key: str
    ) -> RefundResult:
        existing_id = self._refunds_by_idempotency_key.get(idempotency_key)
        if existing_id is not None:
            data = self.refunds[existing_id]
            return RefundResult(
                provider_refund_id=existing_id,
                amount_cents=data["amount_cents"],
                status=data["status"],
            )

        provider_refund_id = f"re_fake_{self._next_refund_id}"
        self._next_refund_id += 1
        self.refunds[provider_refund_id] = {
            "amount_cents": amount_cents,
            "status": "succeeded",
            "provider_payment_id": provider_payment_id,
        }
        self._refunds_by_idempotency_key[idempotency_key] = provider_refund_id
        return RefundResult(
            provider_refund_id=provider_refund_id,
            amount_cents=amount_cents,
            status="succeeded",
        )

    def set_intent_status(self, provider_payment_id: str, status: str) -> None:
        self.intents[provider_payment_id]["status"] = status

    def verify_and_parse_webhook(
        self, *, payload: bytes, signature_header: str
    ) -> WebhookEvent:
        if signature_header != self.valid_signature:
            raise InvalidWebhookSignatureError()
        data = json.loads(payload)
        return WebhookEvent(
            event_id=data["event_id"],
            event_type=data["event_type"],
            provider_payment_id=data["provider_payment_id"],
        )


@pytest.fixture
def fake_order_repository_for_payments() -> FakeOrderRepositoryForPayments:
    return FakeOrderRepositoryForPayments()


@pytest.fixture
def order_service_for_payments(fake_order_repository_for_payments) -> OrderService:
    # cart_repository / product_repository are unused by anything
    # PaymentService calls on OrderService, so they're left unset.
    return OrderService(fake_order_repository_for_payments, None, None)


@pytest.fixture
def fake_payment_repository() -> FakePaymentRepository:
    return FakePaymentRepository()


@pytest.fixture
def fake_payment_provider() -> FakePaymentProvider:
    return FakePaymentProvider()


@pytest.fixture
def payment_service(
    fake_payment_repository, order_service_for_payments, fake_payment_provider
) -> PaymentService:
    return PaymentService(
        fake_payment_repository, order_service_for_payments, fake_payment_provider
    )
