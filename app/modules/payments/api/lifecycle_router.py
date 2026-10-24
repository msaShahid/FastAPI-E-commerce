import contextlib
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.core.database import DbSession
from app.core.jobs.models import BackgroundJob
from app.modules.audit.dependencies.audit_deps import AuditServiceDep
from app.modules.auth.dependencies.auth import AdminUser
from app.modules.orders.models.order import Order
from app.modules.orders.repositories.order_repository import OrderRepository
from app.modules.orders.services.currency_remediation import OrderCurrencyRemediationService
from app.modules.payments.dependencies.lifecycle_deps import LifecycleServiceDep
from app.modules.payments.dependencies.payment_deps import PaymentServiceDep, get_payment_repository
from app.modules.payments.job_types import SWEEP_STALE_ORDERS
from app.modules.payments.repositories.refund_repository import DisputeRepository
from app.modules.payments.schemas.payment import (
    DeferredEventRead,
    DisputeRead,
    EstablishCurrencyRequest,
    EstablishCurrencyResult,
    LifecycleHealth,
    ReconciliationRead,
    ReconciliationResolve,
    RefundRead,
    UnknownCurrencyOrderRead,
    WebhookEventRead,
)
from app.shared.enums.order_status import OrderStatus
from app.shared.pagination.schemas import PageParams, PaginatedResponse

lifecycle_router = APIRouter(prefix="/admin", tags=["order-payment-lifecycle"])


@lifecycle_router.get("/refunds", response_model=PaginatedResponse[RefundRead])
async def list_refunds(
    _admin: AdminUser, lifecycle: LifecycleServiceDep, params: PageParams = Depends(), status_filter: str | None = None
):
    items, total = await lifecycle.refunds.list(status=status_filter, offset=params.offset, limit=params.page_size)
    return PaginatedResponse(items=[RefundRead.model_validate(i) for i in items], total=total,
                             page=params.page, page_size=params.page_size)


@lifecycle_router.get("/reconciliations", response_model=PaginatedResponse[ReconciliationRead])
async def list_reconciliations(
    _admin: AdminUser, lifecycle: LifecycleServiceDep, params: PageParams = Depends(),
    status_filter: str | None = "open", kind: str | None = None,
):
    """Things about money that need a human: late or unmatched payments, mismatches, unconfirmed cancellations
    and refunds. Defaults to the open ones."""
    items, total = await lifecycle.reconciliations.list(
        status=status_filter, kind=kind, offset=params.offset, limit=params.page_size
    )
    return PaginatedResponse(items=[ReconciliationRead.model_validate(i) for i in items], total=total,
                             page=params.page, page_size=params.page_size)


@lifecycle_router.post("/reconciliations/{record_id}/resolve", response_model=ReconciliationRead)
async def resolve_reconciliation(
    record_id: UUID, payload: ReconciliationResolve, admin: AdminUser, lifecycle: LifecycleServiceDep
):
    """Closes a record with a written note (e.g. "refunded in the Stripe dashboard"). Changes no order or money."""
    record = await lifecycle.resolve_reconciliation(record_id, admin.id, payload.resolution, payload.note)
    return ReconciliationRead.model_validate(record)


@lifecycle_router.post(
    "/reconciliations/{record_id}/refund", response_model=RefundRead, status_code=status.HTTP_202_ACCEPTED
)
async def refund_from_reconciliation(record_id: UUID, admin: AdminUser, lifecycle: LifecycleServiceDep):
    """The explicit, administrator-initiated way to refund a late payment. Nothing is ever refunded automatically."""
    _record, refund = await lifecycle.refund_from_reconciliation(record_id, admin.id)
    return RefundRead.model_validate(refund)


@lifecycle_router.get("/lifecycle/health", response_model=LifecycleHealth)
async def lifecycle_health(_admin: AdminUser, lifecycle: LifecycleServiceDep, db: DbSession) -> LifecycleHealth:
    """Monitoring for order expiry and refunds: is the worker sweeping, is anything stuck?"""
    settings = get_settings()
    interval = settings.order_expiry_sweep_interval_seconds
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=settings.pending_order_timeout_minutes)
    pending = Order.status == OrderStatus.PENDING
    stale = (await db.execute(select(func.count()).select_from(Order).where(pending, Order.created_at < cutoff))).scalar_one()
    oldest = (await db.execute(select(func.min(Order.created_at)).where(pending))).scalar_one()
    failed_jobs = (await db.execute(
        select(func.count()).select_from(BackgroundJob).where(BackgroundJob.status == "failed"))).scalar_one()
    last_sweep = (await db.execute(
        select(func.max(BackgroundJob.finished_at)).where(
            BackgroundJob.job_type == SWEEP_STALE_ORDERS, BackgroundJob.status == "succeeded"))).scalar_one()
    now = datetime.now(UTC)
    overdue = interval > 0 and (
        (last_sweep is None and stale > 0) or (last_sweep is not None and (now - last_sweep).total_seconds() > 3 * interval)
    )
    active = {}
    for state in ("requested", "pending", "uncertain"):
        _items, count = await lifecycle.refunds.list(status=state, offset=0, limit=1)
        active[state] = count
    return LifecycleHealth(
        stale_pending_orders=stale,
        oldest_pending_age_seconds=None if oldest is None else int((now.replace(tzinfo=None) - oldest).total_seconds()),
        open_reconciliations=await lifecycle.reconciliations.count_open(),
        active_refunds=active,
        failed_jobs=failed_jobs,
        sweep_interval_seconds=interval,
        last_sweep_succeeded_at=last_sweep,
        sweep_overdue=overdue,
    )


@lifecycle_router.get("/health/dependencies")
async def dependency_health(_admin: AdminUser, db: DbSession) -> dict:
    """For operators and monitoring (admin only; the public /health says nothing about internals): is the database reachable, is
    Redis, and has the background worker finished any job recently? `status` is "ok" only when all three are fine."""
    import time

    from redis.asyncio import Redis

    settings = get_settings()
    started = time.perf_counter()
    try:
        await db.execute(text("SELECT 1"))
        database = {"ok": True, "latency_ms": round((time.perf_counter() - started) * 1000)}
    except Exception as exc:
        database = {"ok": False, "error": type(exc).__name__}

    started = time.perf_counter()
    client = Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
    try:
        await client.ping()
        redis = {"ok": True, "latency_ms": round((time.perf_counter() - started) * 1000)}
    except Exception as exc:
        redis = {"ok": False, "error": type(exc).__name__}
    finally:
        with contextlib.suppress(Exception):
            await client.aclose()

    worker: dict = {"ok": None, "note": "the order-expiry sweep is disabled, so worker liveness cannot be inferred"}
    interval = settings.order_expiry_sweep_interval_seconds
    if interval > 0 and database["ok"]:
        last = (await db.execute(select(func.max(BackgroundJob.finished_at)))).scalar_one()
        limit = 3 * max(interval, 60)
        age = None if last is None else int((datetime.now(UTC) - last).total_seconds())
        worker = {"ok": age is not None and age <= limit, "last_job_finished_seconds_ago": age, "alert_after_seconds": limit}
    healthy = database["ok"] and redis["ok"] and worker["ok"] is not False
    return {"status": "ok" if healthy else "degraded", "database": database, "redis": redis, "worker": worker}


@lifecycle_router.get("/webhook-events", response_model=PaginatedResponse[WebhookEventRead])
async def list_webhook_events(
    _admin: AdminUser, db: DbSession, params: PageParams = Depends(), status_filter: str | None = None
):
    """Every accepted webhook and its outcome: `processed`, `ignored` (a type we do not act on, kept for
    investigation) or `failed` (will be retried by the provider; `attempts` and `last_error` say why)."""
    items, total = await get_payment_repository(db).list_webhook_events(
        status=status_filter, offset=params.offset, limit=params.page_size
    )
    return PaginatedResponse(items=[WebhookEventRead.model_validate(i) for i in items], total=total,
                             page=params.page, page_size=params.page_size)


@lifecycle_router.get("/disputes", response_model=PaginatedResponse[DisputeRead])
async def list_disputes(
    _admin: AdminUser, db: DbSession, params: PageParams = Depends(), status_filter: str | None = None
):
    items, total = await DisputeRepository(db).list(status=status_filter, offset=params.offset, limit=params.page_size)
    return PaginatedResponse(items=[DisputeRead.model_validate(i) for i in items], total=total,
                             page=params.page, page_size=params.page_size)


def _remediation(db, audit) -> OrderCurrencyRemediationService:
    from app.modules.payments.repositories.payment_repository import PaymentRepository

    return OrderCurrencyRemediationService(OrderRepository(db), PaymentRepository(db), audit)


@lifecycle_router.get("/orders/unknown-currency", response_model=PaginatedResponse[UnknownCurrencyOrderRead])
async def list_orders_with_unknown_currency(
    _admin: AdminUser, db: DbSession, audit: AuditServiceDep, params: PageParams = Depends()
):
    """Legacy orders created before currencies were recorded. Nothing is assumed: payments and refunds on them are
    blocked until an administrator establishes the currency from reliable records."""
    rows, total = await _remediation(db, audit).report(offset=params.offset, limit=params.page_size)
    return PaginatedResponse(items=[UnknownCurrencyOrderRead.model_validate(r) for r in rows], total=total,
                             page=params.page, page_size=params.page_size)


@lifecycle_router.post("/orders/{order_id}/establish-currency", response_model=EstablishCurrencyResult)
async def establish_order_currency(
    order_id: UUID, payload: EstablishCurrencyRequest, admin: AdminUser, db: DbSession, audit: AuditServiceDep
):
    """A reviewer's decision on a legacy order, recorded with the evidence, reviewer, time and remediation (audited).
    Evidence that conflicts, is not specific to the order, or is ambiguous leaves the currency UNRESOLVED (the attempt
    is recorded and the order stays flagged for manual review). It converts nothing."""
    record = await _remediation(db, audit).review(
        order_id, admin.id, decision=payload.decision, currency=payload.currency, evidence_type=payload.evidence_type,
        evidence_reference=payload.evidence_reference, note=payload.note, reason=payload.reason,
        store_history_unambiguous=payload.store_history_unambiguous,
    )
    return EstablishCurrencyResult(
        order_id=record.order_id, decision=record.decision, currency=record.currency, evidence_type=record.evidence_type,
        evidence_reference=record.evidence_reference, remediation=record.remediation,
        reviewer_user_id=record.established_by_user_id, reviewed_at=record.created_at,
    )


@lifecycle_router.post("/orders/{order_id}/cancel-and-refund", response_model=RefundRead, status_code=status.HTTP_202_ACCEPTED)
async def cancel_paid_order_with_refund(order_id: UUID, admin: AdminUser, payments: PaymentServiceDep):
    """The dedicated workflow for cancelling a PAID (not yet fulfilled) order. It REQUESTS a full refund; the order is
    only marked refunded, and its stock released exactly once, after the provider CONFIRMS the refund. Until then
    nothing changes. A fulfilled order is refunded with POST /payments/{order_id}/refund (no automatic restock)."""
    refund, _created = await payments.request_refund(order_id=order_id, admin_user_id=admin.id, cancels_order=True)
    return RefundRead.model_validate(refund)


@lifecycle_router.get("/webhook-events/deferred", response_model=PaginatedResponse[DeferredEventRead])
async def list_deferred_webhook_events(
    _admin: AdminUser, db: DbSession, params: PageParams = Depends(), status_filter: str | None = None
):
    """Events that arrived before their payment record and are being retried within bounds (`pending`), were applied
    when it appeared (`recovered`), or gave up (`expired`: its reconciliation record is for a human)."""
    items, total = await get_payment_repository(db).list_deferred(
        status=status_filter, offset=params.offset, limit=params.page_size
    )
    return PaginatedResponse(items=[DeferredEventRead.model_validate(i) for i in items], total=total,
                             page=params.page, page_size=params.page_size)
