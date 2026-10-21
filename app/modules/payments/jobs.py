"""Background jobs started by payments."""

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.email import get_email_sender
from app.core.jobs.registry import job_handler
from app.modules.auth.models.user import User
from app.modules.orders.repositories.order_repository import OrderRepository

logger = logging.getLogger(__name__)

from app.core.jobs.schedule import register_schedule  # noqa: E402
from app.modules.payments.job_types import (  # noqa: E402
    EXECUTE_REFUND,
    EXPIRE_ORDER,
    REPROCESS_DEFERRED_EVENTS,
    SWEEP_STALE_ORDERS,
    expire_order_dedupe_key,
)

SEND_ORDER_CONFIRMATION = "send_order_confirmation"


def order_confirmation_dedupe_key(order_id: UUID) -> str:
    return f"order-confirmation:{order_id}"


@job_handler(SEND_ORDER_CONFIRMATION)
async def send_order_confirmation(
    payload: dict, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """
    Emails the customer that their payment was received. Queued by the Stripe
    webhook in the same transaction that marks the order PAID.

    Idempotent enough: running twice would send two emails, which is why the
    job is queued with a dedupe key and why this only reads the database.
    """
    order_id = UUID(payload["order_id"])

    async with session_factory() as session:
        order = await OrderRepository(session).get_by_id(order_id)
        if order is None:
            logger.warning("order %s no longer exists; no confirmation sent", order_id)
            return
        if order.guest_email:
            recipient = order.guest_email
        else:
            user = await session.get(User, order.user_id)
            if user is None:
                logger.warning("owner of order %s no longer exists; nothing sent", order_id)
                return
            recipient = user.email
        lines = [f"  {item.quantity} x {item.product_name_snapshot}" for item in order.items]
        total = f"{order.total_cents / 100:.2f}"
        short_id = str(order.id)[:8]

    body = "\n".join(
        [
            "Thank you -- we have received your payment.",
            "",
            f"Order {short_id}",
            *lines,
            "",
            f"Total paid: {total}",
        ]
    )
    await get_email_sender().send(
        to=recipient, subject=f"Payment received for order {short_id}", body=body
    )


# ---------------------------------------------------------------------------------------------
# Order expiry and refunds. Every handler is safe to run twice and to be retried after a crash.
# ---------------------------------------------------------------------------------------------
def _build_lifecycle(session):
    """A lifecycle service bound to one job session (commit = this session's commit)."""
    from app.core.jobs.queue import JobQueue
    from app.modules.audit.dependencies.audit_deps import get_audit_repository, get_audit_service
    from app.modules.payments.dependencies.payment_deps import get_payment_provider
    from app.modules.payments.repositories.payment_repository import PaymentRepository
    from app.modules.payments.repositories.refund_repository import (
        DisputeRepository,
        ReconciliationRepository,
        RefundRepository,
    )
    from app.modules.payments.services.lifecycle_service import OrderLifecycleService
    from app.modules.products.repositories.product_repository import ProductRepository

    return OrderLifecycleService(
        OrderRepository(session), ProductRepository(session), PaymentRepository(session), RefundRepository(session),
        ReconciliationRepository(session), get_payment_provider(),
        audit=get_audit_service(get_audit_repository(session)), job_queue=JobQueue(session), commit=session.commit,
        disputes=DisputeRepository(session),
    )


@job_handler(SWEEP_STALE_ORDERS)
async def sweep_stale_orders(payload: dict, session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Scheduled. Finds unpaid orders past PENDING_ORDER_TIMEOUT_MINUTES and queues one expiry job per order.
    It changes nothing itself, so it is harmless to run twice."""
    import time
    from datetime import UTC, datetime, timedelta

    from app.core.config import get_settings
    from app.core.jobs.queue import JobQueue

    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=get_settings().pending_order_timeout_minutes)
    bucket = int(time.time() // 3600)
    async with session_factory() as session, session.begin():
        ids = await OrderRepository(session).list_stale_pending_order_ids(older_than=cutoff)
        queue = JobQueue(session)
        for order_id in ids:
            await queue.enqueue(EXPIRE_ORDER, {"order_id": str(order_id)}, dedupe_key=expire_order_dedupe_key(order_id, bucket))
    logger.info("order expiry sweep: %d stale order(s) queued", len(ids))


@job_handler(EXPIRE_ORDER)
async def expire_order(payload: dict, session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Cancels ONE stale unpaid order safely: the payment intents are cancelled at the provider first, then the
    order is cancelled and restocked together. If the provider's state cannot be confirmed the order is left
    pending and this job fails so it is retried (and a reconciliation record is already open)."""
    from datetime import UTC, datetime, timedelta

    from app.core.cache import flush_pending
    from app.core.config import get_settings
    from app.shared.enums.order_status import OrderStatus

    order_id = UUID(payload["order_id"])
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=get_settings().pending_order_timeout_minutes)
    async with session_factory() as session:
        order = await OrderRepository(session).get_by_id(order_id)
        if order is None or order.status != OrderStatus.PENDING or order.created_at >= cutoff:
            return  # paid, cancelled, or no longer stale since the sweep looked: nothing to do
        result = await _build_lifecycle(session).cancel_order(
            order_id, actor_user_id=None, source="expiry", reason="unpaid order expired"
        )
        await session.commit()
        await flush_pending(session)  # the restock changed product stock: drop the cached product lists
    logger.info("order %s expiry outcome: %s", order_id, result.outcome)
    if result.outcome == "uncertain":
        raise RuntimeError(f"order {order_id}: payment provider state unconfirmed; will retry")


@job_handler(EXECUTE_REFUND)
async def execute_refund(payload: dict, session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Asks the provider for a refund WITHOUT any database transaction open, then records the provider's
    verified answer. The provider idempotency key makes a retry (or a crash after the provider acted)
    return the same refund instead of creating a second one."""
    from app.core.payments.exceptions import PaymentProviderError

    refund_id = UUID(payload["refund_id"])
    async with session_factory() as session:
        attempt = await _build_lifecycle(session).begin_refund_attempt(refund_id)  # commits before returning
    if attempt is None:
        return  # already handled

    try:
        result = await get_provider().create_refund(
            provider_payment_id=attempt.provider_payment_id, amount_cents=attempt.amount,
            currency=attempt.currency, idempotency_key=attempt.idempotency_key,
        )
    except PaymentProviderError as exc:  # includes a timeout: the outcome is UNKNOWN, not "failed"
        async with session_factory() as session:
            verdict = await _build_lifecycle(session).record_refund_attempt_failed(refund_id, str(exc))
            await session.commit()
        logger.warning("refund %s: provider outcome unknown (%s); verdict=%s", refund_id, exc, verdict)
        if verdict == "retry":
            raise  # the job framework retries with backoff; the same idempotency key is reused
        return

    async with session_factory() as session:
        await _build_lifecycle(session).finish_refund(refund_id, result)
        await session.commit()
    logger.info("refund %s recorded with provider status %s", refund_id, result.status)


def get_provider():
    from app.modules.payments.dependencies.payment_deps import get_payment_provider

    return get_payment_provider()


@job_handler(REPROCESS_DEFERRED_EVENTS)
async def reprocess_deferred_events(payload: dict, session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Scheduled. Re-applies webhook events that arrived before their payment record existed. BOUNDED: each event is
    tried at most WEBHOOK_RECOVERY_MAX_ATTEMPTS times and only within WEBHOOK_RECOVERY_WINDOW_MINUTES of arriving, then it
    is `expired` (its reconciliation record stays for a human). Idempotent and safe with several workers (SKIP LOCKED)."""
    from datetime import UTC, datetime, timedelta

    from app.core.config import get_settings
    from app.core.jobs.queue import JobQueue
    from app.core.payments.interface import WebhookEvent
    from app.modules.audit.dependencies.audit_deps import get_audit_repository, get_audit_service
    from app.modules.cart.repositories.cart_repository import CartRepository
    from app.modules.orders.services.order_service import OrderService
    from app.modules.payments.dependencies.payment_deps import get_payment_provider
    from app.modules.payments.repositories.payment_repository import PaymentRepository
    from app.modules.payments.services.payment_service import PaymentService
    from app.modules.products.repositories.product_repository import ProductRepository

    settings = get_settings()
    recovered = expired = retried = 0
    async with session_factory() as session:
        rows = await PaymentRepository(session).claim_due_deferred(limit=50)
        audit = get_audit_service(get_audit_repository(session))
        service = PaymentService(
            PaymentRepository(session),
            OrderService(OrderRepository(session), CartRepository(session), ProductRepository(session)),
            get_payment_provider(), job_queue=JobQueue(session), audit=audit,
            lifecycle=_build_lifecycle(session), commit=session.commit,
        )
        for row in rows:
            now = datetime.now(UTC)
            try:
                async with session.begin_nested():  # one event's failure never affects the others
                    if await service.repository.get_by_provider_payment_id(row.provider_payment_id) is None:
                        row.attempts += 1
                        if row.attempts >= settings.webhook_recovery_max_attempts or now >= row.expires_at:
                            row.status, row.resolved_at = "expired", now
                            row.last_reason = "gave up: the payment record never appeared within the recovery bounds"
                            await audit.log(actor_user_id=None, action="webhook.deferred_event_expired",
                                            target_type="webhook_event", target_id=row.event_id,
                                            details={"provider": row.provider, "event_type": row.event_type,
                                                     "attempts": row.attempts})
                            expired += 1
                        else:
                            row.next_attempt_at = now + timedelta(seconds=max(settings.webhook_recovery_interval_seconds, 1))
                            retried += 1
                        continue
                    await service.apply_deferred_event(WebhookEvent(
                        row.event_id, row.event_type, row.provider_payment_id, row.amount, row.amount_received, row.currency,
                        row.provider_refund_id, row.refund_status, row.provider_dispute_id, row.dispute_status,
                        row.dispute_reason, row.provider,
                    ))
                    row.attempts += 1
                    row.status, row.resolved_at = "recovered", now
                    row.last_reason = "applied once the payment record appeared"
                    for key in (f"unmatched_payment:{row.provider_payment_id}", f"unknown_refund:{row.provider_refund_id}",
                                f"unmatched_dispute:{row.provider_dispute_id}"):
                        await service.lifecycle.reconciliations.resolve_by_dedupe_key(
                            key, "Recovered automatically: the payment record appeared and the event was applied")
                    await audit.log(actor_user_id=None, action="webhook.deferred_event_recovered", target_type="webhook_event",
                                    target_id=row.event_id, details={"provider": row.provider, "event_type": row.event_type})
                    recovered += 1
            except Exception as exc:  # recorded, counted, and still bounded by the attempt limit
                await session.refresh(row)
                row.attempts += 1
                row.last_reason = f"{type(exc).__name__}: {exc}"[:300]
                if row.attempts >= settings.webhook_recovery_max_attempts:
                    row.status, row.resolved_at = "expired", now
                else:
                    row.next_attempt_at = now + timedelta(seconds=max(settings.webhook_recovery_interval_seconds, 1))
                logger.warning("deferred event %s could not be applied: %s", row.event_id, type(exc).__name__)
        await session.commit()
    if rows:
        logger.info("deferred webhook events: %d recovered, %d retried later, %d expired", recovered, retried, expired)


# The worker itself schedules the expiry sweep (every ORDER_EXPIRY_SWEEP_INTERVAL_SECONDS; 0 disables it).
register_schedule(SWEEP_STALE_ORDERS, lambda settings: settings.order_expiry_sweep_interval_seconds)
register_schedule(REPROCESS_DEFERRED_EVENTS, lambda settings: settings.webhook_recovery_interval_seconds)
