from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from app.core.config import get_settings
from app.core.database import DbSession
from app.core.jobs import JobQueue
from app.core.payments.interface import PaymentProvider
from app.core.payments.stripe_provider import StripePaymentProvider
from app.modules.audit.dependencies.audit_deps import AuditServiceDep
from app.modules.orders.dependencies.order_deps import get_order_service
from app.modules.orders.services.order_service import OrderService
from app.modules.payments.repositories.payment_repository import PaymentRepository
from app.modules.payments.services.payment_service import PaymentService


def get_payment_repository(db: DbSession) -> PaymentRepository:
    return PaymentRepository(db)


@lru_cache
def get_payment_provider() -> PaymentProvider:
    settings = get_settings()
    return StripePaymentProvider(
        secret_key=settings.stripe_secret_key,
        webhook_secret=settings.stripe_webhook_secret,
    )


def get_payment_service(
    repository: Annotated[PaymentRepository, Depends(get_payment_repository)],
    order_service: Annotated[OrderService, Depends(get_order_service)],
    provider: Annotated[PaymentProvider, Depends(get_payment_provider)],
    db: DbSession,
    audit: AuditServiceDep,
) -> PaymentService:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.modules.orders.repositories.order_repository import OrderRepository
    from app.modules.payments.repositories.refund_repository import (
        DisputeRepository,
        ReconciliationRepository,
        RefundRepository,
    )
    from app.modules.payments.services.lifecycle_service import OrderLifecycleService
    from app.modules.payments.services.provider_resolver import ProviderResolver
    from app.modules.payments.webhook_recovery import record_failed_event
    from app.modules.products.repositories.product_repository import ProductRepository

    lifecycle = OrderLifecycleService(
        OrderRepository(db), ProductRepository(db), repository, RefundRepository(db),
        ReconciliationRepository(db), provider, audit=audit, job_queue=JobQueue(db), commit=db.commit,
        disputes=DisputeRepository(db), provider_resolver=ProviderResolver(db),
    )
    # A failed webhook attempt is recorded through the SAME engine this request uses (never another database).
    failure_sessions = async_sessionmaker(db.bind, expire_on_commit=False)

    async def record_failure(event, error):
        await record_failed_event(failure_sessions, event, error)

    return PaymentService(
        repository, order_service, provider, job_queue=JobQueue(db), audit=audit, lifecycle=lifecycle,
        commit=db.commit, rollback=db.rollback, failure_recorder=record_failure, resolver=ProviderResolver(db),
    )


PaymentServiceDep = Annotated[PaymentService, Depends(get_payment_service)]
