import logging
from uuid import UUID

from app.core.payments.interface import PaymentProvider, WebhookEvent
from app.modules.orders.exceptions.order_exceptions import (
    InvalidStatusTransitionError,
    OrderNotFoundError,
)
from app.modules.orders.models.order import Order
from app.modules.orders.services.order_service import OrderService
from app.modules.payments.exceptions.payment_exceptions import (
    OrderNotPayableError,
    OrderNotRefundableError,
)
from app.modules.payments.models.payment import Payment
from app.modules.payments.repositories.payment_repository import PaymentRepository
from app.shared.enums.order_status import OrderStatus
from app.shared.enums.payment_status import PaymentStatus

logger = logging.getLogger(__name__)

DEFAULT_CURRENCY = "usd"

SUCCEEDED_EVENT = "payment_intent.succeeded"
FAILED_EVENT = "payment_intent.payment_failed"

OPEN_INTENT_STATUSES = frozenset(
    {
        "requires_payment_method",
        "requires_confirmation",
        "requires_action",
        "processing",
        "requires_capture",
    }
)

class PaymentService:
    def __init__(
        self,
        repository: PaymentRepository,
        order_service: OrderService,
        provider: PaymentProvider,
        currency: str = DEFAULT_CURRENCY,
    ) -> None:
        self.repository = repository
        self.order_service = order_service
        self.provider = provider
        self.currency = currency

    async def create_payment_for_order(
        self, *, order_id: UUID, user_id: UUID
    ) -> tuple[Payment, str]:

        order = await self.order_service.get_order(
            order_id=order_id, user_id=user_id, is_admin=False
        )

        order = await self.order_service.lock_order_for_update(order.id)
        self._assert_order_payable(order)

        existing_payments = await self.repository.list_for_order(order.id)
        if any(p.status == PaymentStatus.SUCCEEDED for p in existing_payments):
            raise OrderNotPayableError("This order has already been paid")

        reusable = next(
            (
                p
                for p in existing_payments
                if p.status in (PaymentStatus.PENDING, PaymentStatus.FAILED)
            ),
            None,
        )
        if reusable is not None:
            intent = await self.provider.retrieve_payment_intent(
                provider_payment_id=reusable.provider_payment_id
            )

            if intent.status == "succeeded":

                if reusable.status != PaymentStatus.SUCCEEDED:
                    await self.repository.update_status(
                        reusable, PaymentStatus.SUCCEEDED
                    )
                raise OrderNotPayableError("This order has already been paid")

            if intent.status in OPEN_INTENT_STATUSES:

                if reusable.status != PaymentStatus.PENDING:
                    await self.repository.update_status(reusable, PaymentStatus.PENDING)
                return reusable, intent.client_secret

            if reusable.status != PaymentStatus.FAILED:
                await self.repository.update_status(reusable, PaymentStatus.FAILED)

        idempotency_key = f"order:{order.id}:intent:{len(existing_payments)}"

        intent = await self.provider.create_payment_intent(
            amount_cents=order.total_cents,
            currency=self.currency,
            metadata={"order_id": str(order.id)},
            idempotency_key=idempotency_key,
        )
        payment = await self.repository.create(
            order_id=order.id,
            provider_payment_id=intent.provider_payment_id,
            amount_cents=order.total_cents,
            currency=self.currency,
        )
        return payment, intent.client_secret

    async def refund_payment(
        self, *, order_id: UUID, admin_user_id: UUID
    ) -> tuple[Payment, Order]:

        order = await self.order_service.get_by_id_unchecked(order_id)
        if order is None:
            raise OrderNotFoundError(order_id)

        order = await self.order_service.lock_order_for_update(order.id)

        if order.status not in (OrderStatus.PAID, OrderStatus.FULFILLED):
            raise OrderNotRefundableError(
                f"Cannot refund an order in status {order.status.value}"
            )

        payments = await self.repository.list_for_order(order.id)
        succeeded_payment = next(
            (p for p in payments if p.status == PaymentStatus.SUCCEEDED), None
        )
        if succeeded_payment is None:
            raise OrderNotRefundableError("No successful payment found for this order")

        idempotency_key = f"order:{order.id}:refund:{succeeded_payment.id}"

        refund = await self.provider.create_refund(
            provider_payment_id=succeeded_payment.provider_payment_id,
            amount_cents=succeeded_payment.amount_cents,
            idempotency_key=idempotency_key,
        )

        succeeded_payment = await self.repository.mark_refunded(
            succeeded_payment,
            provider_refund_id=refund.provider_refund_id,
            refunded_amount_cents=refund.amount_cents,
        )

        updated_order = await self.order_service.update_status(
            order_id=order.id,
            new_status=OrderStatus.REFUNDED,
            changed_by_user_id=admin_user_id,
        )
        return succeeded_payment, updated_order

    async def handle_webhook(self, *, payload: bytes, signature_header: str) -> None:
        event = self.provider.verify_and_parse_webhook(
            payload=payload, signature_header=signature_header
        )
        await self.process_webhook_event(event)

    async def process_webhook_event(self, event: WebhookEvent) -> None:

        claimed = await self.repository.claim_webhook_event(event.event_id)
        if not claimed:
            return

        if event.event_type == SUCCEEDED_EVENT:
            await self._handle_payment_succeeded(event.provider_payment_id)
        elif event.event_type == FAILED_EVENT:
            await self._handle_payment_failed(event.provider_payment_id)

    async def _handle_payment_succeeded(self, provider_payment_id: str) -> None:
        payment = await self.repository.get_by_provider_payment_id(provider_payment_id)
        if payment is None:
            logger.warning(
                "payment_intent.succeeded for unknown provider_payment_id=%s",
                provider_payment_id,
            )
            return

        if payment.status != PaymentStatus.SUCCEEDED:
            await self.repository.update_status(payment, PaymentStatus.SUCCEEDED)

        order = await self.order_service.get_by_id_unchecked(payment.order_id)
        if order is None:
            logger.warning(
                "payment %s succeeded but its order %s no longer exists",
                payment.id,
                payment.order_id,
            )
            return

        if order.status != OrderStatus.PENDING:

            logger.warning(
                "payment %s succeeded for order %s but order.status=%s "
                "(not PENDING) -- order was NOT transitioned to PAID; "
                "this likely needs a manual refund",
                payment.id,
                order.id,
                order.status.value,
            )
            return

        try:
            await self.order_service.update_status(
                order_id=order.id,
                new_status=OrderStatus.PAID,
                changed_by_user_id=None,
            )
        except InvalidStatusTransitionError:
            logger.warning(
                "payment %s succeeded but order %s could not transition " "%s -> paid",
                payment.id,
                order.id,
                order.status.value,
            )
            return

    async def _handle_payment_failed(self, provider_payment_id: str) -> None:
        payment = await self.repository.get_by_provider_payment_id(provider_payment_id)
        if payment is None or payment.status == PaymentStatus.SUCCEEDED:
            return
        await self.repository.update_status(payment, PaymentStatus.FAILED)

    @staticmethod
    def _assert_order_payable(order: Order) -> None:
        if order.status != OrderStatus.PENDING:
            raise OrderNotPayableError(
                f"Cannot pay an order in status {order.status.value}"
            )
