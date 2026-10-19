"""
The ONE place that changes an order's lifecycle with side effects: cancel/expire, fulfil, confirm
payment, request/execute/confirm a refund.

Rules this service enforces (and the database re-checks with triggers):
  * pending -> paid | cancelled ; paid -> fulfilled | refunded ; fulfilled -> refunded ; the rest is final.
  * An order becomes `paid` only from a verified, succeeded payment; `refunded` only from a
    provider-CONFIRMED refund; `cancelled` only together with its (one-time) stock release.
  * Every provider call happens with NO database transaction open: each operation commits before
    it talks to the provider, then re-locks and re-checks what it assumed afterwards.
  * An unknown provider outcome is never treated as "it failed": nothing is cancelled or refunded,
    and a reconciliation record is opened for a human.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from app.core.config import get_settings
from app.core.jobs.queue import JobQueue
from app.core.money import to_stripe_amount
from app.core.payments.exceptions import PaymentProviderError
from app.core.payments.interface import PaymentProvider, RefundResult
from app.modules.audit.services.audit_service import AuditService
from app.modules.orders.exceptions.order_exceptions import (
    CancellationNotAllowedError,
    InvalidStatusTransitionError,
    OrderNotFoundError,
    PaymentInProgressError,
)
from app.modules.orders.models.order import Order
from app.modules.orders.repositories.order_repository import OrderRepository
from app.modules.payments.exceptions.payment_exceptions import OrderNotRefundableError
from app.modules.payments.job_types import EXECUTE_REFUND, execute_refund_dedupe_key
from app.modules.payments.models.payment import Payment
from app.modules.payments.models.refund import Refund
from app.modules.payments.repositories.payment_repository import PaymentRepository
from app.modules.payments.repositories.refund_repository import (
    ReconciliationRepository,
    RefundRepository,
)
from app.modules.products.repositories.product_repository import ProductRepository
from app.shared.enums.lifecycle import ReconciliationKind, RefundStatus
from app.shared.enums.order_status import OrderStatus
from app.shared.enums.payment_status import PaymentStatus

logger = logging.getLogger(__name__)


@dataclass
class CancelResult:
    outcome: str  # "cancelled" | "already_cancelled" | "uncertain" | "payment_in_progress"
    order: Order


@dataclass(frozen=True)
class RefundAttempt:
    refund_id: UUID
    provider_payment_id: str
    amount: int
    currency: str
    idempotency_key: str


class OrderLifecycleService:
    def __init__(
        self,
        orders: OrderRepository,
        products: ProductRepository,
        payments: PaymentRepository,
        refunds: RefundRepository,
        reconciliations: ReconciliationRepository,
        provider: PaymentProvider,
        *,
        audit: AuditService | None = None,
        job_queue: JobQueue | None = None,
        commit=None,
        disputes=None,
    ) -> None:
        self.orders = orders
        self.products = products
        self.payments = payments
        self.refunds = refunds
        self.reconciliations = reconciliations
        self.provider = provider
        self.audit = audit
        self.job_queue = job_queue
        self._commit_hook = commit  # None in unit tests with in-memory fakes
        self.disputes = disputes  # DisputeRepository; None only in unit tests that do not touch disputes

    # ------------------------------------------------------------ small helpers
    async def _commit(self) -> None:
        if self._commit_hook is not None:
            await self._commit_hook()

    async def _audit(self, action: str, target_type: str, target_id, actor_user_id=None, details=None) -> None:
        if self.audit is not None:
            await self.audit.log(
                actor_user_id=actor_user_id, action=action, target_type=target_type,
                target_id=target_id, details=details,
            )

    async def reconcile(self, kind: ReconciliationKind, dedupe_key: str, *, payment: Payment | None = None,
                        order_id=None, provider_payment_id: str | None = None, details: dict | None = None) -> bool:
        """Opens a reconciliation record once per dedupe_key (redelivered events never duplicate it)."""
        recorded = await self.reconciliations.record(
            kind=kind.value, dedupe_key=dedupe_key,
            payment_id=payment.id if payment else None,
            order_id=order_id or (payment.order_id if payment else None),
            provider_payment_id=provider_payment_id or (payment.provider_payment_id if payment else None),
            details=details or {},
        )
        if recorded:
            await self._audit(f"reconciliation.{kind.value}", "payment_reconciliation", dedupe_key, details=details)
        return recorded

    async def _lock_order(self, order_id: UUID) -> Order:
        order = await self.orders.get_by_id_for_update(order_id)
        if order is None:
            raise OrderNotFoundError(order_id)
        return order

    # ------------------------------------------------------------ payment confirmation
    async def mark_paid(self, order: Order, payment: Payment) -> bool:
        """pending -> paid, from a payment that is already recorded as SUCCEEDED. `order` must be locked."""
        if order.status != OrderStatus.PENDING:
            return False
        await self.orders.update_status(order, OrderStatus.PAID, None, "payment confirmed by the payment provider")
        await self._audit("order.paid", "order", order.id, details={"payment_id": str(payment.id)})
        return True

    # ------------------------------------------------------------ fulfilment (admin)
    async def fulfil_order(self, order_id: UUID, admin_user_id: UUID) -> Order:
        order = await self._lock_order(order_id)
        if order.status == OrderStatus.FULFILLED:
            return order  # a retry changes nothing
        if order.status != OrderStatus.PAID:
            raise InvalidStatusTransitionError(order.status.value, OrderStatus.FULFILLED.value)
        await self.orders.update_status(order, OrderStatus.FULFILLED, admin_user_id, "marked fulfilled by an administrator")
        await self._audit("order.fulfilled", "order", order.id, admin_user_id)
        return order

    # ------------------------------------------------------------ cancellation and expiry
    async def cancel_order(self, order_id: UUID, *, actor_user_id: UUID | None, source: str, reason: str) -> CancelResult:
        """Cancels an UNPAID order: payment intents made unpayable first, then stock released, in one step.

        Phase 1 (transaction): lock, decide, commit.   Phase 2 (no transaction): ask the provider to
        cancel each open intent.   Phase 3 (transaction): re-lock and re-check, then cancel + restock.
        """
        order = await self._lock_order(order_id)
        if order.status == OrderStatus.CANCELLED:
            return CancelResult("already_cancelled", order)
        if order.status != OrderStatus.PENDING:
            raise CancellationNotAllowedError(order.status.value)
        payments = await self.payments.list_for_order(order.id)
        if any(p.status == PaymentStatus.SUCCEEDED for p in payments):
            raise PaymentInProgressError()
        to_cancel = [(p.id, p.provider_payment_id) for p in payments if p.status != PaymentStatus.REFUNDED]
        await self._commit()  # release the row lock BEFORE talking to the provider

        uncertain: list[str] = []
        completed = False
        for payment_id, provider_payment_id in to_cancel:
            try:
                result = await self.provider.cancel_payment_intent(
                    provider_payment_id=provider_payment_id, idempotency_key=f"cancel:{payment_id}"
                )
            except PaymentProviderError:  # includes a timeout: the outcome is UNKNOWN
                uncertain.append(provider_payment_id)
                continue
            if result.status == "succeeded":
                completed = True
            elif result.status != "canceled":
                uncertain.append(provider_payment_id)  # e.g. still processing: cannot be made unpayable
        if completed:
            return CancelResult("payment_in_progress", order)
        if uncertain:
            await self.reconcile(
                ReconciliationKind.CANCEL_UNCERTAIN, f"cancel_uncertain:{order_id}", order_id=order_id,
                details={"provider_payment_ids": uncertain, "source": source},
            )
            await self._commit()
            return CancelResult("uncertain", order)

        order = await self._lock_order(order_id)  # fresh state: a webhook may have won the race meanwhile
        if order.status == OrderStatus.CANCELLED:
            return CancelResult("already_cancelled", order)
        if order.status != OrderStatus.PENDING:
            return CancelResult("payment_in_progress", order)
        payments = await self.payments.list_for_order(order.id)
        if any(p.status == PaymentStatus.SUCCEEDED for p in payments):
            return CancelResult("payment_in_progress", order)
        for payment in payments:
            if payment.status == PaymentStatus.PENDING:
                await self.payments.update_status(payment, PaymentStatus.FAILED)  # its intent is now cancelled
        await self._release_stock_and_cancel(order, actor_user_id, reason)
        await self._audit("order.cancelled", "order", order.id, actor_user_id, {"source": source, "reason": reason})
        return CancelResult("cancelled", order)

    async def _release_stock_and_cancel(self, order: Order, actor_user_id, reason: str) -> None:
        """Restock exactly once (guarded by the row lock AND stock_released_at) and cancel together."""
        if order.stock_released_at is None:
            locked = await self.products.get_many_for_update([item.product_id for item in order.items])
            restock: dict[int, int] = {}
            for item in order.items:
                if item.product_id in locked:  # a deleted product has nothing to restock
                    restock[item.product_id] = restock.get(item.product_id, 0) + item.quantity
            await self.products.adjust_stock_many(locked, restock)
            order.stock_released_at = datetime.now(UTC)
        await self.orders.update_status(order, OrderStatus.CANCELLED, actor_user_id, reason)

    # ------------------------------------------------------------ refunds
    async def request_refund(self, order_id: UUID, requested_by_user_id: UUID, *, payment_id: UUID | None = None) -> tuple[Refund, bool]:
        """Records that a refund was REQUESTED and queues its execution. Nothing is refunded yet and the
        order is NOT marked refunded. Returns (refund, created); asking twice returns the same refund."""
        order = await self._lock_order(order_id)
        payments = await self.payments.list_for_order(order.id)
        if payment_id is None:
            if order.status not in (OrderStatus.PAID, OrderStatus.FULFILLED):
                raise OrderNotRefundableError(f"Cannot refund an order in status {order.status.value}")
            payment = next((p for p in payments if p.status == PaymentStatus.SUCCEEDED), None)
        else:  # explicit payment (e.g. a late payment on a cancelled order, from a reconciliation record)
            payment = next((p for p in payments if p.id == payment_id), None)
            if payment is not None and payment.status != PaymentStatus.SUCCEEDED:
                payment = None
        if payment is None:
            raise OrderNotRefundableError("No successful payment found for this order")
        existing = await self.refunds.get_active_for_payment(payment.id)
        if existing is not None:
            return existing, False
        if order.currency is not None and order.currency != payment.currency:
            from app.modules.payments.exceptions.payment_exceptions import PaymentInconsistentError

            raise PaymentInconsistentError("The payment currency does not match the order currency")
        if self.disputes is not None and await self.disputes.has_open(payment.id):
            from app.modules.payments.exceptions.payment_exceptions import PaymentDisputedError

            raise PaymentDisputedError()
        # Refund what is still refundable: never more than the payment, counting refunds in flight, confirmed,
        # and any made at the provider. (Partial refunds requested through NovaCart remain a business decision.)
        remaining = payment.amount_cents - await self.refunds.committed_amount(payment.id)
        if remaining <= 0:
            raise OrderNotRefundableError("This payment has already been fully refunded")
        amount = to_stripe_amount(remaining, payment.currency)  # validates currency and amount
        attempt_number = await self.refunds.count_for_payment(payment.id)
        refund = await self.refunds.create(
            payment_id=payment.id, order_id=order.id, amount_cents=amount, currency=payment.currency,
            idempotency_key=f"refund:{payment.id}:{attempt_number}", requested_by_user_id=requested_by_user_id,
        )
        if self.job_queue is not None:
            await self.job_queue.enqueue(EXECUTE_REFUND, {"refund_id": str(refund.id)},
                                         dedupe_key=execute_refund_dedupe_key(refund.id))
        await self._audit("refund.requested", "refund", refund.id, requested_by_user_id,
                          {"order_id": str(order.id), "amount": amount, "currency": payment.currency})
        return refund, True

    async def begin_refund_attempt(self, refund_id: UUID) -> RefundAttempt | None:
        """Phase 1 of executing a refund: count the attempt durably, then release every lock."""
        refund = await self.refunds.get(refund_id, for_update=True)
        if refund is None or refund.status != RefundStatus.REQUESTED:
            return None  # already handled (duplicate job): nothing to do
        payment = await self.payments.get_by_id(refund.payment_id)
        refund.attempts += 1
        await self.refunds.save(refund)
        attempt = RefundAttempt(
            refund.id, payment.provider_payment_id, to_stripe_amount(refund.amount_cents, refund.currency),
            refund.currency, refund.idempotency_key,
        )
        await self._commit()
        return attempt

    async def record_refund_attempt_failed(self, refund_id: UUID, error: str) -> str:
        """The provider call failed or timed out: its outcome is unknown. Returns "retry" or "uncertain"."""
        refund = await self.refunds.get(refund_id, for_update=True)
        if refund is None or refund.status != RefundStatus.REQUESTED:
            return "stale"
        if refund.attempts < get_settings().refund_max_attempts:
            return "retry"
        refund.status = RefundStatus.UNCERTAIN.value
        refund.failure_reason = error[:500]
        await self.refunds.save(refund)
        await self.reconcile(ReconciliationKind.REFUND_UNCERTAIN, f"refund_uncertain:{refund.id}", order_id=refund.order_id,
                             details={"refund_id": str(refund.id), "error": error[:500]})
        await self._audit("refund.uncertain", "refund", refund.id, details={"error": error[:500]})
        await self._commit()
        return "uncertain"

    async def finish_refund(self, refund_id: UUID, result: RefundResult) -> None:
        refund = await self.refunds.get(refund_id, for_update=True)
        if refund is None:
            return
        await self.apply_refund_state(
            refund, status=result.status, amount=result.amount_cents, currency=result.currency,
            provider_refund_id=result.provider_refund_id,
        )
        await self._commit()

    async def apply_refund_state(self, refund: Refund, *, status: str | None, amount: int | None,
                                 currency: str | None, provider_refund_id: str | None) -> None:
        """Applies a PROVIDER-reported refund state (from our request's result or a webhook). Idempotent.
        Only a provider-confirmed `succeeded` can mark a refund (and then the order) as refunded."""
        if amount is not None and amount != refund.amount_cents or currency not in (None, refund.currency):
            await self.reconcile(
                ReconciliationKind.REFUND_MISMATCH, f"refund_mismatch:{refund.id}", order_id=refund.order_id,
                details={"requested_amount": refund.amount_cents, "requested_currency": refund.currency,
                         "provider_amount": amount, "provider_currency": currency},
            )
            await self._audit("payment.refund_mismatch", "refund", refund.id,
                              details={"requested": refund.amount_cents, "provider": amount})
        if provider_refund_id and refund.provider_refund_id is None:
            refund.provider_refund_id = provider_refund_id
        if refund.status in (RefundStatus.SUCCEEDED, RefundStatus.FAILED):
            await self.refunds.save(refund)
            return  # final: a redelivered event changes nothing
        if status == "succeeded":
            refund.status = RefundStatus.SUCCEEDED.value
            await self.refunds.save(refund)
            await self._confirm_refund(refund, provider_refund_id or refund.provider_refund_id, amount)
        elif status in ("failed", "canceled"):
            refund.status = RefundStatus.FAILED.value
            refund.failure_reason = f"provider reported '{status}'"
            await self.refunds.save(refund)
            await self._audit("refund.failed", "refund", refund.id, details={"provider_status": status})
        else:  # pending / requires_action / anything unrecognised: accepted but not final
            if refund.status == RefundStatus.REQUESTED:
                refund.status = RefundStatus.PENDING.value
            await self.refunds.save(refund)

    async def _confirm_refund(self, refund: Refund, provider_refund_id: str | None, amount: int | None) -> None:
        order = await self._lock_order(refund.order_id)
        payment = await self.payments.get_by_id(refund.payment_id)
        await self.payments.add_refunded_amount(
            payment, provider_refund_id=provider_refund_id, amount=amount or refund.amount_cents
        )
        fully_refunded = (payment.refunded_amount_cents or 0) >= payment.amount_cents
        if refund.origin == "requested" and fully_refunded and order.status in (OrderStatus.PAID, OrderStatus.FULFILLED):
            await self.orders.update_status(order, OrderStatus.REFUNDED, None, "refund confirmed by the payment provider")
        await self._audit("refund.succeeded", "refund", refund.id, details={"order_id": str(order.id)})

    # ------------------------------------------------------------ admin status changes
    async def admin_change_status(self, order_id: UUID, new_status: OrderStatus, admin_user_id: UUID) -> Order:
        """The ONLY admin status mutation. Anything that would fabricate a payment, claim an unconfirmed refund
        or cancel without payment/stock handling is refused with an explanation of the correct route."""
        from app.modules.orders.exceptions.order_exceptions import StatusChangeNotAllowedError

        if new_status == OrderStatus.FULFILLED:
            return await self.fulfil_order(order_id, admin_user_id)
        reasons = {
            OrderStatus.CANCELLED: (
                "Cancel an unpaid order with POST /api/v1/orders/{id}/cancel: it also cancels the payment "
                "and restocks. A paid order is refunded, not cancelled."
            ),
            OrderStatus.PAID: "An order becomes paid only when the payment provider confirms a payment; it cannot be set by hand.",
            OrderStatus.REFUNDED: (
                "An order becomes refunded only when the payment provider confirms a refund. "
                "Request one with POST /api/v1/payments/{order_id}/refund."
            ),
            OrderStatus.PENDING: "An order cannot return to pending.",
        }
        raise StatusChangeNotAllowedError(reasons[new_status])

    # ------------------------------------------------------------ reconciliation (admin)
    async def resolve_reconciliation(self, record_id: UUID, admin_user_id: UUID, resolution: str, note: str):
        from app.modules.payments.exceptions.payment_exceptions import ReconciliationNotOpenError

        record = await self.reconciliations.get(record_id, for_update=True)
        if record is None or record.status != "open":
            raise ReconciliationNotOpenError()
        record.status = "resolved"
        record.resolution = resolution
        record.resolution_note = note.strip()
        record.resolved_by_user_id = admin_user_id
        record.resolved_at = datetime.now(UTC).replace(tzinfo=None)
        await self.reconciliations.save(record)
        await self._audit("reconciliation.resolved", "payment_reconciliation", record.id, admin_user_id,
                          {"resolution": resolution})
        return record

    async def refund_from_reconciliation(self, record_id: UUID, admin_user_id: UUID):
        """The explicit, admin-initiated way to refund a late/unmatched payment. Never automatic."""
        from app.modules.payments.exceptions.payment_exceptions import ReconciliationNotOpenError

        record = await self.reconciliations.get(record_id, for_update=True)
        if record is None or record.status != "open":
            raise ReconciliationNotOpenError()
        if record.payment_id is None or record.order_id is None:
            raise OrderNotRefundableError("This record has no payment to refund")
        refund, _ = await self.request_refund(record.order_id, admin_user_id, payment_id=record.payment_id)
        record.status = "resolved"
        record.resolution = "refund_requested"
        record.resolution_note = f"Refund {refund.id} requested by an administrator"
        record.resolved_by_user_id = admin_user_id
        record.resolved_at = datetime.now(UTC).replace(tzinfo=None)
        await self.reconciliations.save(record)
        await self._audit("reconciliation.resolved", "payment_reconciliation", record.id, admin_user_id,
                          {"resolution": "refund_requested", "refund_id": str(refund.id)})
        return record, refund

    # ------------------------------------------------------------ refunds made at the provider
    async def record_external_refund(self, payment: Payment, *, provider_refund_id: str, amount: int | None,
                                     currency: str | None) -> bool:
        """A CONFIRMED refund that did not start in NovaCart (e.g. made in the provider's dashboard) is recorded so the
        payment's refunded total is true and the cumulative limit holds. It never changes the order (that is a
        business decision): an open reconciliation record tells a human. Returns whether it was recorded."""
        from sqlalchemy.exc import IntegrityError

        if await self.refunds.get_by_provider_refund_id(provider_refund_id) is not None:
            return False
        details = {"provider_refund_id": provider_refund_id, "amount": amount, "currency": currency,
                   "payment_amount": payment.amount_cents, "payment_currency": payment.currency}
        if amount is None or amount <= 0 or currency != payment.currency:
            await self.reconcile(ReconciliationKind.REFUND_MISMATCH, f"external_refund_invalid:{provider_refund_id}",
                                 payment=payment, details=details)
            return False
        try:
            async with self.refunds.db.begin_nested():  # the cumulative trigger may refuse it
                await self.refunds.create(
                    payment_id=payment.id, order_id=payment.order_id, amount_cents=amount, currency=currency,
                    status="succeeded", origin="provider", provider_refund_id=provider_refund_id,
                    idempotency_key=f"provider:{provider_refund_id}",
                )
        except IntegrityError:
            await self.reconcile(ReconciliationKind.REFUND_MISMATCH, f"external_refund_exceeds:{provider_refund_id}",
                                 payment=payment, details={**details, "problem": "would exceed the payment amount"})
            return False
        await self.payments.add_refunded_amount(payment, provider_refund_id=provider_refund_id, amount=amount)
        await self.reconcile(ReconciliationKind.EXTERNAL_REFUND, f"external_refund:{provider_refund_id}",
                             payment=payment, details=details)
        await self._audit("refund.external_recorded", "payment", payment.id, details=details)
        return True

    # ------------------------------------------------------------ disputes
    async def apply_dispute_event(self, event) -> None:
        """Records a provider dispute (chargeback) as explicit, audited transitions. It changes no order, stock or
        refund; it opens a reconciliation record so a human responds. A closed dispute never reopens, so events that
        arrive out of order are safe."""
        payment = (
            await self.payments.get_by_provider_payment_id(event.provider_payment_id)
            if event.provider_payment_id else None
        )
        if payment is None or not event.provider_dispute_id:
            await self.reconcile(
                ReconciliationKind.UNMATCHED_DISPUTE, f"unmatched_dispute:{event.provider_dispute_id or event.event_id}",
                provider_payment_id=event.provider_payment_id or None,
                details={"provider_dispute_id": event.provider_dispute_id, "status": event.dispute_status,
                         "amount": event.amount},
            )
            return
        mapped = DISPUTE_STATUS_MAP.get(event.dispute_status or "", "open")  # unknown statuses need attention: open
        terminal = mapped in ("won", "lost", "closed")
        dispute = await self.disputes.get_by_provider_id(event.provider, event.provider_dispute_id, for_update=True)
        if dispute is None:
            dispute = await self.disputes.create(
                provider=event.provider, provider_dispute_id=event.provider_dispute_id, payment_id=payment.id,
                order_id=payment.order_id, amount_cents=event.amount or payment.amount_cents,
                currency=event.currency or payment.currency, status=mapped, provider_status=event.dispute_status,
                reason=event.dispute_reason, closed_at=datetime.now(UTC).replace(tzinfo=None) if terminal else None,
            )
            await self._audit("dispute.opened", "dispute", dispute.id, details={"status": mapped, "payment_id": str(payment.id)})
            if not terminal:
                await self.reconcile(ReconciliationKind.DISPUTE_OPENED, f"dispute_opened:{dispute.id}", payment=payment,
                                     details={"dispute_id": str(dispute.id), "reason": event.dispute_reason,
                                              "amount": dispute.amount_cents, "currency": dispute.currency})
        elif dispute.status in ("won", "lost", "closed") or mapped == dispute.status:
            return  # final, or nothing new: a late/duplicate/out-of-order event changes nothing
        else:
            before = dispute.status
            dispute.status = mapped
            dispute.provider_status = event.dispute_status
            if terminal:
                dispute.closed_at = datetime.now(UTC).replace(tzinfo=None)
            await self.disputes.save(dispute)
            await self._audit("dispute.status_changed", "dispute", dispute.id, details={"from": before, "to": mapped})
        if mapped == "lost":
            await self.reconcile(ReconciliationKind.DISPUTE_LOST, f"dispute_lost:{dispute.id}", payment=payment,
                                 details={"dispute_id": str(dispute.id), "amount": dispute.amount_cents,
                                          "currency": dispute.currency})
            await self._audit("dispute.lost", "dispute", dispute.id)
        elif mapped in ("won", "closed"):
            await self._audit(f"dispute.{mapped}", "dispute", dispute.id)


# Provider status -> our status. Statuses we do not recognise are treated as "open" (needs attention).
DISPUTE_STATUS_MAP = {
    "needs_response": "open",
    "warning_needs_response": "open",
    "under_review": "under_review",
    "warning_under_review": "under_review",
    "won": "won",
    "lost": "lost",
    "warning_closed": "closed",
}
