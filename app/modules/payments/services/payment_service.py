import logging
from uuid import UUID

from app.core.config import get_settings
from app.core.jobs import JobQueue
from app.core.money import (
    CurrencyNotPayableError,
    InvalidMoneyError,
    UnsupportedCurrencyError,
    get_currency,
    to_stripe_amount,
)
from app.core.payments.exceptions import PaymentProviderError
from app.core.payments.interface import PaymentProvider, WebhookEvent
from app.modules.audit.services.audit_service import AuditService
from app.modules.orders.models.order import Order
from app.modules.orders.services.order_service import OrderService
from app.modules.payments.exceptions.payment_exceptions import (
    LivePaymentsDisabledError,
    OrderNotPayableError,
    PaymentInconsistentError,
)
from app.modules.payments.jobs import SEND_ORDER_CONFIRMATION, order_confirmation_dedupe_key
from app.modules.payments.models.payment import Payment
from app.modules.payments.repositories.payment_repository import PaymentRepository
from app.modules.payments.services.lifecycle_service import OrderLifecycleService
from app.shared.enums.lifecycle import ReconciliationKind
from app.shared.enums.order_status import OrderStatus
from app.shared.enums.payment_status import PaymentStatus

logger = logging.getLogger(__name__)

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
        job_queue: JobQueue | None = None,
        audit: AuditService | None = None,
        lifecycle: OrderLifecycleService | None = None,
        commit=None,
        rollback=None,
        failure_recorder=None,
    ) -> None:
        self.repository = repository
        self.order_service = order_service
        self.provider = provider
        # Slow follow-up work (emails ...) is queued, never done inside the
        # webhook request. None only in unit tests that do not care.
        self.job_queue = job_queue
        # Payment events that need a human (late payment, unmatched payment) are written to the
        # audit log so operations can list them via GET /audit-logs, not only to a log line.
        self.audit = audit
        # Order/payment lifecycle operations (cancel, refund, mark paid) and the commit hook used to end
        # a transaction BEFORE any provider call. Both None only in unit tests with in-memory fakes.
        self.lifecycle = lifecycle
        self._commit_hook = commit
        # Used only when handling a webhook fails: end the failed transaction, then record the failure separately.
        self._rollback_hook = rollback
        self._failure_recorder = failure_recorder

    async def _commit(self) -> None:
        if self._commit_hook is not None:
            await self._commit_hook()

    @staticmethod
    def _assert_live_payments_allowed() -> None:
        """A live Stripe key is refused unless LIVE_PAYMENTS_ENABLED is set. Test keys are unaffected."""
        settings = get_settings()
        if settings.stripe_secret_key.startswith(("sk_live_", "rk_live_")) and not settings.live_payments_enabled:
            raise LivePaymentsDisabledError()

    def _assert_currency_payable(self, code: str):
        """The currency must be known, verified for the integration AND enabled for this account."""
        currency = get_currency(code)  # unknown code -> 422 currency_unsupported
        if not self.provider.supports_currency(currency.code):
            raise CurrencyNotPayableError(currency.code, "payment_currency_not_verified")
        if currency.code not in get_settings().enabled_payment_currencies():
            raise CurrencyNotPayableError(currency.code, "payment_currency_not_enabled")
        return currency

    async def _flag_for_review(
        self, action: str, payment_id, details: dict, *, kind: ReconciliationKind | None = None,
        order_id=None, provider_payment_id: str | None = None, dedupe_key: str | None = None,
    ) -> None:
        """Audit-logs the event and, when a `kind` is given, opens ONE reconciliation record for it."""
        if self.audit is not None:
            await self.audit.log(
                actor_user_id=None, action=action, target_type="payment", target_id=payment_id, details=details
            )
        if kind is not None and self.lifecycle is not None:
            key = dedupe_key or f"{kind.value}:{payment_id or provider_payment_id}"
            await self.lifecycle.reconcile(
                kind, key, order_id=order_id, provider_payment_id=provider_payment_id or details.get("provider_payment_id"),
                payment=None if payment_id is None else await self.repository.get_by_id(payment_id), details=details,
            )

    async def _plan_payment(self, order_id: UUID):
        """Phase 1 (one short transaction): lock the order, validate everything, decide what to do, then
        COMMIT so no lock is held while we talk to the payment provider."""
        order = await self.order_service.lock_order_for_update(order_id)
        self._assert_order_payable(order)

        # The payment is built from the ORDER's own currency and total, never from a global
        # setting, so a later change of store configuration can't alter what is charged.
        if order.currency is None:
            raise OrderNotPayableError("This order has no recorded currency and cannot be paid")
        currency = self._assert_currency_payable(order.currency)
        stripe_amount = to_stripe_amount(order.total_cents, currency.code)

        existing_payments = await self.repository.list_for_order(order.id)
        if any(p.status == PaymentStatus.SUCCEEDED for p in existing_payments):
            raise OrderNotPayableError("This order has already been paid")
        reusable = next(
            (p for p in existing_payments if p.status in (PaymentStatus.PENDING, PaymentStatus.FAILED)), None
        )
        if reusable is not None and (
            reusable.currency != currency.code or reusable.amount_cents != order.total_cents
        ):
            raise PaymentInconsistentError("The existing payment does not match the order's amount/currency")
        plan = {
            "currency": currency.code,
            "stripe_amount": stripe_amount,
            "total": order.total_cents,
            "reuse_id": reusable.id if reusable else None,
            "reuse_provider_id": reusable.provider_payment_id if reusable else None,
            # One Stripe idempotency key per (order, attempt number): a retry or a concurrent request gets
            # the SAME intent back instead of creating a second one.
            "key": f"order:{order.id}:intent:{len(existing_payments)}",
        }
        await self._commit()
        return plan

    async def create_payment_for_order(
        self, *, order_id: UUID, user_id: UUID
    ) -> tuple[Payment, str]:
        """
        Paying for an order is always a self-service action: an admin cannot start a payment on
        someone else's order (is_admin=False is deliberate here, not a placeholder).

        Three phases, none of which holds a database transaction while waiting on the provider:
        plan (locked, then committed) -> provider call -> re-lock, re-check, record.
        """
        self._assert_live_payments_allowed()
        order = await self.order_service.get_order(order_id=order_id, user_id=user_id, is_admin=False)
        plan = await self._plan_payment(order.id)

        if plan["reuse_provider_id"] is not None:
            intent = await self.provider.retrieve_payment_intent(provider_payment_id=plan["reuse_provider_id"])
            locked = await self.order_service.lock_order_for_update(order.id)
            self._assert_order_payable(locked)  # it may have been cancelled/paid while we asked the provider
            reusable = await self.repository.get_by_id(plan["reuse_id"])
            if intent.status == "succeeded":
                # Stripe says paid but our webhook hasn't landed yet (or was lost): refuse a second
                # intent rather than risk double-charging; the webhook is what confirms payment.
                if reusable.status != PaymentStatus.SUCCEEDED:
                    await self.repository.update_status(reusable, PaymentStatus.SUCCEEDED)
                raise OrderNotPayableError("This order has already been paid")
            if intent.status in OPEN_INTENT_STATUSES:
                if reusable.status != PaymentStatus.PENDING:
                    await self.repository.update_status(reusable, PaymentStatus.PENDING)
                return reusable, intent.client_secret
            # Terminal on the provider's side (e.g. canceled): record it and create a fresh intent.
            if reusable.status != PaymentStatus.FAILED:
                await self.repository.update_status(reusable, PaymentStatus.FAILED)
            await self._commit()

        return await self._create_new_payment(order.id, plan)

    async def _create_new_payment(self, order_id: UUID, plan: dict) -> tuple[Payment, str]:
        intent = await self.provider.create_payment_intent(  # no transaction is open here
            amount_cents=plan["stripe_amount"],
            currency=plan["currency"],
            metadata={"order_id": str(order_id)},
            idempotency_key=plan["key"],
        )
        order = await self.order_service.lock_order_for_update(order_id)
        try:
            self._assert_order_payable(order)
            if any(p.status == PaymentStatus.SUCCEEDED for p in await self.repository.list_for_order(order_id)):
                raise OrderNotPayableError("This order has already been paid")
        except OrderNotPayableError:
            # The order was cancelled or paid while the intent was being created: that intent must not
            # stay payable. Commit first so no lock is held while we cancel it at the provider.
            await self._commit()
            await self._cancel_orphan_intent(intent.provider_payment_id, order_id)
            raise
        existing = await self.repository.get_by_provider_payment_id(intent.provider_payment_id)
        if existing is not None:  # a concurrent request got the same intent (same idempotency key)
            return existing, intent.client_secret
        payment = await self.repository.create(
            order_id=order_id,
            provider_payment_id=intent.provider_payment_id,
            amount_cents=plan["total"],
            currency=plan["currency"],
            provider=self.provider.name,  # so the payment stays reconcilable if provider settings change later
        )
        return payment, intent.client_secret

    async def _cancel_orphan_intent(self, provider_payment_id: str, order_id: UUID) -> None:
        try:
            await self.provider.cancel_payment_intent(
                provider_payment_id=provider_payment_id, idempotency_key=f"cancel-orphan:{provider_payment_id}"
            )
        except PaymentProviderError:
            await self._flag_for_review(
                "payment.orphan_intent", None, {"provider_payment_id": provider_payment_id},
                kind=ReconciliationKind.CANCEL_UNCERTAIN, order_id=order_id, provider_payment_id=provider_payment_id,
            )
            await self._commit()

    async def request_refund(
        self, *, order_id: UUID, admin_user_id: UUID, payment_id: UUID | None = None, cancels_order: bool = False
    ):
        """Admin-only. Records that a refund was REQUESTED and queues it. Nothing has been refunded yet and
        the order is not marked refunded: that only happens when the provider CONFIRMS the refund
        (see OrderLifecycleService.apply_refund_state). Asking twice returns the same refund."""
        self._assert_live_payments_allowed()
        return await self.lifecycle.request_refund(
            order_id, admin_user_id, payment_id=payment_id, cancels_order=cancels_order
        )

    async def handle_webhook(self, *, payload: bytes, signature_header: str) -> None:
        event = self.provider.verify_and_parse_webhook(
            payload=payload, signature_header=signature_header
        )
        try:
            await self.process_webhook_event(event)
        except Exception as exc:
            # Nothing this attempt did persists (its transaction rolls back); the provider will retry. Record the
            # failed attempt in its own transaction so it is visible and the retry is recognised as a retry.
            if self._rollback_hook is not None and self._failure_recorder is not None:
                await self._rollback_hook()
                try:
                    await self._failure_recorder(event, exc)
                except Exception:
                    logger.exception("could not record the failed webhook event %s", event.event_id)
            raise

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
        claimed = await self.repository.claim_webhook_event(event)
        if not claimed:
            return  # a duplicate of an event already processed (or being processed)

        handled = True
        if event.event_type == SUCCEEDED_EVENT:
            await self._handle_payment_succeeded(event)
        elif event.event_type == FAILED_EVENT:
            await self._handle_payment_failed(event.provider_payment_id)
        elif event.event_type.startswith("refund."):
            await self._handle_refund_event(event)
        elif event.event_type.startswith("charge.dispute."):
            await self._handle_dispute_event(event)
        else:
            handled = False  # a type we do not act on: kept as `ignored` so it can be investigated
        await self.repository.finish_webhook_event(event, "processed" if handled else "ignored")

    async def _event_matches_payment(self, event: WebhookEvent, payment: Payment) -> bool:
        """Verifies the provider's amount and currency against OUR stored payment and order.

        Anything missing or different is recorded for review and the payment is NOT marked
        succeeded: a mismatch is never treated as success.
        """
        details = {"provider_payment_id": event.provider_payment_id, "payment_id": str(payment.id)}
        if event.amount is None or event.currency is None:
            await self._flag_for_review(
                "payment.unverifiable_success", payment.id, details, kind=ReconciliationKind.UNVERIFIABLE_PAYMENT
            )
            return False
        try:
            expected = to_stripe_amount(payment.amount_cents, payment.currency)
        except (CurrencyNotPayableError, InvalidMoneyError, UnsupportedCurrencyError):
            expected = None
        received_ok = event.amount_received is None or event.amount_received == expected
        if expected is None or event.amount != expected or not received_ok or event.currency != payment.currency:
            await self._flag_for_review(
                "payment.amount_mismatch",
                payment.id,
                {
                    **details,
                    "expected_amount": expected,
                    "expected_currency": payment.currency,
                    "received_amount": event.amount,
                    "received_amount_received": event.amount_received,
                    "received_currency": event.currency,
                },
                kind=ReconciliationKind.AMOUNT_MISMATCH,
            )
            return False
        order = await self.order_service.get_by_id_unchecked(payment.order_id)
        if order is not None and (
            order.total_cents != payment.amount_cents or order.currency not in (None, payment.currency)
        ):
            await self._flag_for_review(
                "payment.order_mismatch",
                payment.id,
                {**details, "order_total": order.total_cents, "order_currency": order.currency},
                kind=ReconciliationKind.ORDER_MISMATCH,
            )
            return False
        return True

    async def _handle_payment_succeeded(self, event: WebhookEvent) -> None:
        provider_payment_id = event.provider_payment_id
        payment = await self.repository.get_by_provider_payment_id(provider_payment_id)
        if payment is None:
            logger.warning("payment_intent.succeeded for unknown provider_payment_id=%s", provider_payment_id)
            await self._flag_for_review(
                "payment.unmatched_success", None, {"provider_payment_id": provider_payment_id},
                kind=ReconciliationKind.UNMATCHED_PAYMENT, provider_payment_id=provider_payment_id,
            )
            await self._defer_event(event, "no payment record for this provider payment id yet")
            return

        if not await self._event_matches_payment(event, payment):
            return

        # Lock the ORDER row before deciding anything: cancellation/expiry take the same lock, so a
        # payment and a cancellation can never both act on the order's old state.
        order = await self.order_service.repository.get_by_id_for_update(payment.order_id)
        if payment.status != PaymentStatus.SUCCEEDED:
            await self.repository.update_status(payment, PaymentStatus.SUCCEEDED)  # money WAS received
        if order is None:
            logger.warning("payment %s succeeded but its order %s no longer exists", payment.id, payment.order_id)
            await self._flag_for_review(
                "payment.order_missing", payment.id, {"order_id": str(payment.order_id)},
                kind=ReconciliationKind.ORDER_MISMATCH,
            )
            return

        if order.status != OrderStatus.PENDING:
            # Money has been taken for an order that can no longer be paid (cancelled/expired/already
            # paid by another intent). It is NOT moved to PAID and nothing is fulfilled or refunded
            # automatically: an administrator reconciles it (explicitly requesting a refund, or not).
            logger.warning(
                "payment %s succeeded for order %s in status %s: recorded for reconciliation",
                payment.id, order.id, order.status.value,
            )
            await self._flag_for_review(
                "payment.late_unreconciled", payment.id,
                {
                    "order_id": str(order.id),
                    "order_status": order.status.value,
                    "provider_payment_id": provider_payment_id,
                    "amount_cents": payment.amount_cents,
                    "currency": payment.currency,
                },
                kind=ReconciliationKind.LATE_PAYMENT,
            )
            return

        if not await self.lifecycle.mark_paid(order, payment):
            return
        if self.job_queue is not None:
            # Saved in this same transaction: the order is PAID and the email is queued, or (if the
            # commit fails and Stripe re-delivers the webhook) neither happened yet.
            await self.job_queue.enqueue(
                SEND_ORDER_CONFIRMATION,
                {"order_id": str(order.id)},
                dedupe_key=order_confirmation_dedupe_key(order.id),
            )

    async def _handle_dispute_event(self, event: WebhookEvent) -> None:
        await self.lifecycle.apply_dispute_event(event)
        await self._defer_if_payment_unknown(event, "dispute event for a payment we have no record of yet")

    async def _defer_if_payment_unknown(self, event: WebhookEvent, reason: str) -> None:
        if event.provider_payment_id and await self.repository.get_by_provider_payment_id(event.provider_payment_id) is None:
            await self._defer_event(event, reason)

    async def _defer_event(self, event: WebhookEvent, reason: str) -> None:
        """Keeps the event (identifiers only) so the worker can re-apply it, within bounds, once its payment exists."""
        if not event.provider_payment_id:
            return
        settings = get_settings()
        if settings.webhook_recovery_interval_seconds <= 0:
            return  # recovery disabled: the reconciliation record opened for it is the only trace
        await self.repository.defer_event(
            event, window_minutes=settings.webhook_recovery_window_minutes,
            interval_seconds=settings.webhook_recovery_interval_seconds, reason=reason,
        )

    async def apply_deferred_event(self, event: WebhookEvent) -> None:
        """Re-applies a deferred event through the SAME handlers as a live delivery (so every rule applies: amount and
        currency verification, late payments for cancelled orders are only reconciled, refund limits, dispute finality).
        The caller has already established that the payment record exists and holds the row lock."""
        if event.event_type == SUCCEEDED_EVENT:
            await self._handle_payment_succeeded(event)
        elif event.event_type.startswith("refund."):
            await self._handle_refund_event(event)
        elif event.event_type.startswith("charge.dispute."):
            await self.lifecycle.apply_dispute_event(event)

    async def _handle_refund_event(self, event: WebhookEvent) -> None:
        """A provider refund event: applies the provider-reported state to OUR refund (idempotent).
        A refund we did not request (e.g. made in the provider's dashboard) is recorded for review."""
        refund = None
        if event.provider_refund_id:
            refund = await self.lifecycle.refunds.get_by_provider_refund_id(event.provider_refund_id)
        if refund is None and event.provider_payment_id:
            payment = await self.repository.get_by_provider_payment_id(event.provider_payment_id)
            if payment is not None:
                refund = await self.lifecycle.refunds.get_active_for_payment(payment.id)
        if refund is None and event.refund_status == "succeeded" and event.provider_payment_id:
            payment = await self.repository.get_by_provider_payment_id(event.provider_payment_id)
            if payment is not None and event.provider_refund_id:
                await self.lifecycle.record_external_refund(
                    payment, provider_refund_id=event.provider_refund_id, amount=event.amount, currency=event.currency
                )
                return
        if refund is None:
            await self._defer_if_payment_unknown(event, "refund event for a payment we have no record of yet")
            await self._flag_for_review(
                "payment.unknown_refund", None,
                {"provider_refund_id": event.provider_refund_id, "provider_payment_id": event.provider_payment_id,
                 "amount": event.amount, "status": event.refund_status},
                kind=ReconciliationKind.REFUND_MISMATCH, provider_payment_id=event.provider_payment_id,
                dedupe_key=f"unknown_refund:{event.provider_refund_id}",
            )
            return
        locked = await self.lifecycle.refunds.get(refund.id, for_update=True)
        await self.lifecycle.apply_refund_state(
            locked, status=event.refund_status, amount=event.amount, currency=event.currency,
            provider_refund_id=event.provider_refund_id,
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
