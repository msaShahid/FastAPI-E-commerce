import logging
from uuid import UUID

from app.core.config import get_settings
from app.core.jobs import JobQueue
from app.core.payments.interface import PaymentProvider, WebhookEvent
from app.modules.audit.services.audit_service import AuditService
from app.modules.orders.exceptions.order_exceptions import (
    InvalidStatusTransitionError,
    OrderNotFoundError,
)
from app.modules.orders.models.order import Order
from app.modules.orders.services.order_service import OrderService
from app.modules.payments.exceptions.payment_exceptions import (
    LivePaymentsDisabledError,
    OrderNotPayableError,
    OrderNotRefundableError,
)
from app.modules.payments.jobs import SEND_ORDER_CONFIRMATION, order_confirmation_dedupe_key
from app.modules.payments.models.payment import Payment
from app.modules.payments.repositories.payment_repository import PaymentRepository
from app.shared.enums.order_status import OrderStatus
from app.shared.enums.payment_status import PaymentStatus

logger = logging.getLogger(__name__)

DEFAULT_CURRENCY = "usd"

SUCCEEDED_EVENT = "payment_intent.succeeded"
FAILED_EVENT = "payment_intent.payment_failed"

# Stripe PaymentIntent statuses that mean "still open, the customer can
# still complete or retry this exact intent." Anything else (canceled,
# or a status we don't recognize) is treated as terminal, and a fresh
# intent is created instead.
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
        job_queue: JobQueue | None = None,
        audit: AuditService | None = None,
    ) -> None:
        self.repository = repository
        self.order_service = order_service
        self.provider = provider
        self.currency = currency
        # Slow follow-up work (emails ...) is queued, never done inside the
        # webhook request. None only in unit tests that do not care.
        self.job_queue = job_queue
        # Payment events that need a human (late payment, unmatched payment) are written to the
        # audit log so operations can list them via GET /audit-logs, not only to a log line.
        self.audit = audit

    @staticmethod
    def _assert_live_payments_allowed() -> None:
        """A live Stripe key is refused unless LIVE_PAYMENTS_ENABLED is set. Test keys are unaffected."""
        settings = get_settings()
        if settings.stripe_secret_key.startswith(("sk_live_", "rk_live_")) and not settings.live_payments_enabled:
            raise LivePaymentsDisabledError()

    async def _flag_for_review(self, action: str, payment_id, details: dict) -> None:
        if self.audit is not None:
            await self.audit.log(
                actor_user_id=None, action=action, target_type="payment", target_id=payment_id, details=details
            )

    async def create_payment_for_order(
        self, *, order_id: UUID, user_id: UUID
    ) -> tuple[Payment, str]:
        """
        Paying for an order is always a self-service action: an admin
        cannot start a payment on someone else's order (is_admin=False
        is deliberate here, not a placeholder -- admins use a different
        path for anything that touches somebody else's money).
        """
        self._assert_live_payments_allowed()
        order = await self.order_service.get_order(
            order_id=order_id, user_id=user_id, is_admin=False
        )

        # Lock the order row for the rest of this transaction so two
        # concurrent requests for the same order (double-click, retry
        # storm) can't both fall through the "no pending payment yet"
        # check and each create their own Stripe intent. The second
        # caller blocks here until the first transaction commits, then
        # re-reads the now-current state.
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
                # Stripe says paid but our webhook hasn't landed yet (or
                # was lost). Sync locally and refuse a second intent
                # rather than risk double-charging the customer.
                if reusable.status != PaymentStatus.SUCCEEDED:
                    await self.repository.update_status(reusable, PaymentStatus.SUCCEEDED)
                raise OrderNotPayableError("This order has already been paid")

            if intent.status in OPEN_INTENT_STATUSES:
                # The existing intent is still payable -- reuse it
                # instead of creating a second one the customer could
                # also complete.
                if reusable.status != PaymentStatus.PENDING:
                    await self.repository.update_status(reusable, PaymentStatus.PENDING)
                return reusable, intent.client_secret

            # Terminal on Stripe's side (e.g. canceled) and not already
            # marked FAILED locally -- fall through and create a new one.
            if reusable.status != PaymentStatus.FAILED:
                await self.repository.update_status(reusable, PaymentStatus.FAILED)

        # One Stripe idempotency key per (order, attempt number). A crash
        # between "Stripe created the intent" and "we saved the Payment
        # row" is recovered by retrying with the same key: Stripe hands
        # back the *same* intent instead of creating an orphan. A later,
        # genuinely new attempt (previous one recorded as FAILED above)
        # gets a new key because the attempt count has moved on.
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
        """
        Admin-only. Refunds the order's successful payment through Stripe
        first, and only moves the order to REFUNDED once Stripe confirms
        the refund -- the order's status must never claim money moved
        when it didn't. Not reachable for an order that was never paid
        (or one already fulfilled-and-refunded): see ALLOWED_TRANSITIONS.
        """
        self._assert_live_payments_allowed()
        order = await self.order_service.get_by_id_unchecked(order_id)
        if order is None:
            raise OrderNotFoundError(order_id)

        # Lock for the same reason create_payment_for_order does: without
        # it, a concurrent second refund request for the same order could
        # both pass the status/payment checks below before either one
        # updates anything, and Stripe would be asked to refund the same
        # PaymentIntent twice.
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

        # One idempotency key per payment being refunded -- a retried
        # request (client timeout, etc.) hits the same Stripe refund
        # instead of issuing a second one.
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
        """
        Claims the event_id BEFORE running any side effects. Stripe
        guarantees at-least-once delivery, so the same event can arrive
        twice, including concurrently. Claiming first (an atomic
        INSERT ... ON CONFLICT DO NOTHING) means a concurrent duplicate
        blocks on the unique index until the first delivery's
        transaction finishes, then sees it already claimed and returns
        without touching the payment or order -- there is no window
        where both duplicates pass a check and both run the handler.
        """
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
            await self._flag_for_review(
                "payment.unmatched_success", None, {"provider_payment_id": provider_payment_id}
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
            # Money has been taken (Stripe confirms succeeded) for an
            # order that is no longer PENDING -- e.g. it was cancelled,
            # or a second intent for the same order also succeeded. The
            # order will NOT be moved to PAID. This needs a human to
            # look at it (likely a refund), so it must not disappear
            # silently.
            logger.warning(
                "payment %s succeeded for order %s but order.status=%s "
                "(not PENDING) -- order was NOT transitioned to PAID; "
                "this likely needs a manual refund",
                payment.id,
                order.id,
                order.status.value,
            )
            # Money was taken for an order that is not PENDING: record it for a human (no automatic
            # refund or fulfilment is done; that policy is a later decision).
            await self._flag_for_review(
                "payment.late_unreconciled",
                payment.id,
                {
                    "order_id": str(order.id),
                    "order_status": order.status.value,
                    "provider_payment_id": provider_payment_id,
                    "amount_cents": payment.amount_cents,
                    "currency": payment.currency,
                },
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
                "payment %s succeeded but order %s could not transition "
                "%s -> paid",
                payment.id,
                order.id,
                order.status.value,
            )
            return

        if self.job_queue is not None:
            # Saved in this same transaction: the order is PAID and the email
            # is queued, or (if the commit fails and Stripe re-delivers the
            # webhook) neither happened yet.
            await self.job_queue.enqueue(
                SEND_ORDER_CONFIRMATION,
                {"order_id": str(order.id)},
                dedupe_key=order_confirmation_dedupe_key(order.id),
            )

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
