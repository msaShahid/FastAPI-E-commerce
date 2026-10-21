"""
Controlled review of legacy orders whose currency was never recorded.

Nothing here guesses. An order's currency is established only by an administrator who supplies evidence that
demonstrably concerns THAT order, and every review is recorded (reviewer, time, evidence source, decision and the
remediation taken). Evidence that conflicts, is not specific to the order, or is ambiguous leaves the currency
UNRESOLVED (still recorded, the order flagged for manual review): it is never forced through. The currency of a
payment already recorded for the order must agree (payment amounts and currencies are immutable). No amount is
converted.
"""

from uuid import UUID

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.money import get_currency
from app.modules.audit.models.audit_log import AuditLog
from app.modules.audit.services.audit_service import AuditService
from app.modules.orders.exceptions.order_exceptions import (
    CurrencyConflictsWithPaymentError,
    EvidenceNotSpecificToOrderError,
    OrderCurrencyAlreadyKnownError,
    OrderNotFoundError,
    StoreHistoryAmbiguousError,
)
from app.modules.orders.models.currency_remediation import OrderCurrencyRemediation
from app.modules.orders.repositories.order_repository import OrderRepository
from app.modules.payments.repositories.payment_repository import PaymentRepository
from app.modules.store.repositories.store_repository import StoreRepository

CAVEAT = (
    "Payments created before currencies were recorded used a hard-coded default currency, so a payment's recorded "
    "currency is NOT reliable evidence on its own. Confirm against the provider's record or your accounting records."
)


class OrderCurrencyRemediationService:
    def __init__(self, orders: OrderRepository, payments: PaymentRepository, audit: AuditService) -> None:
        self.orders = orders
        self.payments = payments
        self.audit = audit

    # ------------------------------------------------------------ report
    async def report(self, *, offset: int, limit: int):
        """Every order with no recorded currency, its payments, and its review history. `manual_review_required` is
        true when it was reviewed and left unresolved (nothing has established its currency)."""
        orders, total = await self.orders.list_unknown_currency(offset=offset, limit=limit)
        rows = []
        for order in orders:
            payments = await self.payments.list_for_order(order.id)
            reviews = await self.orders.list_currency_reviews(order.id)
            rows.append({
                "order_id": order.id, "status": order.status.value, "total_cents": order.total_cents,
                "created_at": order.created_at,
                "payments": [{"provider": p.provider, "provider_payment_id": p.provider_payment_id,
                              "status": p.status.value, "amount_cents": p.amount_cents, "currency": p.currency}
                             for p in payments],
                "manual_review_required": any(r.decision != "established" for r in reviews),
                "reviews": [self.review_view(r) for r in reviews],
                "caveat": CAVEAT,
            })
        return rows, total

    @staticmethod
    def review_view(record: OrderCurrencyRemediation) -> dict:
        return {
            "decision": record.decision, "currency": record.currency, "evidence_type": record.evidence_type,
            "evidence_reference": record.evidence_reference, "reviewer_user_id": record.established_by_user_id,
            "reviewed_at": record.created_at, "remediation": record.remediation, "note": record.note,
        }

    # ------------------------------------------------------------ review
    async def review(self, order_id: UUID, admin_user_id: UUID, *, decision: str, currency: str | None,
                     evidence_type: str, evidence_reference: str, note: str | None, reason: str | None,
                     store_history_unambiguous: bool) -> OrderCurrencyRemediation:
        order = await self.orders.get_by_id_for_update(order_id)
        if order is None:
            raise OrderNotFoundError(order_id)
        if order.currency is not None:
            raise OrderCurrencyAlreadyKnownError()
        reference = evidence_reference.strip()

        if decision == "unresolved":  # the reviewer concluded the evidence does not settle it
            outcome = "unresolved_conflicting" if reason == "conflicting" else "unresolved_inconclusive"
            return await self._record(order, admin_user_id, outcome, None, evidence_type, reference, note,
                                      f"Left unresolved; manual review required ({reason}): {note}")

        code = get_currency(currency).code  # 422 for an unsupported code
        payments = await self.payments.list_for_order(order.id)
        failure = await self._check(order, payments, code, evidence_type, reference, store_history_unambiguous)
        if failure is not None:
            outcome, error, why = failure
            # Recorded BEFORE the error is returned, so the attempt and its outcome are never lost.
            await self._record(order, admin_user_id, outcome, None, evidence_type, reference, note,
                               f"Left unresolved; manual review required: {why}")
            await self.orders.db.commit()
            raise error
        record = await self._record(order, admin_user_id, "established", code, evidence_type, reference, note,
                                    f"Order currency set to {code}")
        order.currency = code  # NULL -> value is the one change the database allows; amounts are untouched
        await self.orders.db.flush()
        return record

    async def _check(self, order, payments, code, evidence_type, reference, store_history_unambiguous):
        """Returns None when the evidence reliably establishes `code` for THIS order, else (decision, error, why)."""
        for payment in payments:
            if payment.currency != code:  # payment rows are immutable: the two records conflict
                return ("unresolved_conflicting", CurrencyConflictsWithPaymentError(payment.currency),
                        f"a payment was recorded in {payment.currency}, not {code}")
        ref = reference.lower()
        provider_ids = [p.provider_payment_id.lower() for p in payments]
        if evidence_type == "provider_record":
            if not provider_ids:
                why = "this order has no payment, so there is no provider record of it to cite"
                return "unresolved_inconclusive", EvidenceNotSpecificToOrderError(why), why
            if not any(pid in ref for pid in provider_ids):
                why = "the reference does not cite a provider payment id of this order"
                return "unresolved_inconclusive", EvidenceNotSpecificToOrderError(why), why
        elif evidence_type == "accounting_record":
            if str(order.id).lower() not in ref and not any(pid in ref for pid in provider_ids):
                why = "the reference does not cite this order's id or a provider payment id of this order"
                return "unresolved_inconclusive", EvidenceNotSpecificToOrderError(why), why
        else:  # store_history_reviewed: weakest evidence, so it must be unambiguous
            if not store_history_unambiguous:
                why = "the reviewer did not attest that the store history is unambiguous for this order"
                return "unresolved_inconclusive", StoreHistoryAmbiguousError(why), why
            current, _configured = await StoreRepository(self.orders.db).effective_currency(get_settings().default_store_currency)
            if current != code:
                why = f"the store's current currency is {current or 'unset'}, not {code}"
                return "unresolved_inconclusive", StoreHistoryAmbiguousError(why), why
            changed = (await self.orders.db.execute(
                select(func.count()).select_from(AuditLog).where(
                    AuditLog.action == "store.currency_set",
                    AuditLog.details["from"].astext.is_not(None),
                    AuditLog.details["from"].astext != AuditLog.details["to"].astext,
                ))).scalar_one()
            if changed:
                why = "the store currency has been changed since it was first set"
                return "unresolved_inconclusive", StoreHistoryAmbiguousError(why), why
        return None

    async def _record(self, order, admin_user_id, decision, currency, evidence_type, reference, note, remediation):
        record = await self.orders.add_currency_remediation(
            OrderCurrencyRemediation(
                order_id=order.id, decision=decision, currency=currency, evidence_type=evidence_type,
                evidence_reference=reference, note=note, remediation=remediation, established_by_user_id=admin_user_id,
            )
        )
        await self.audit.log(
            actor_user_id=admin_user_id,
            action="order.currency_established" if decision == "established" else "order.currency_review_unresolved",
            target_type="order", target_id=order.id,
            details={"decision": decision, "currency": currency, "evidence_type": evidence_type,
                     "evidence_reference": reference, "remediation": remediation, "total_cents": order.total_cents},
        )
        return record
