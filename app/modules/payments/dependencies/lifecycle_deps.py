from typing import Annotated

from fastapi import Depends

from app.core.database import DbSession
from app.core.jobs import JobQueue
from app.core.payments.interface import PaymentProvider
from app.modules.audit.dependencies.audit_deps import AuditServiceDep
from app.modules.orders.repositories.order_repository import OrderRepository
from app.modules.payments.dependencies.payment_deps import get_payment_provider
from app.modules.payments.repositories.payment_repository import PaymentRepository
from app.modules.payments.repositories.refund_repository import (
    DisputeRepository,
    ReconciliationRepository,
    RefundRepository,
)
from app.modules.payments.services.lifecycle_service import OrderLifecycleService
from app.modules.payments.services.provider_resolver import ProviderResolver
from app.modules.products.repositories.product_repository import ProductRepository


def get_lifecycle_service(
    db: DbSession,
    provider: Annotated[PaymentProvider, Depends(get_payment_provider)],
    audit: AuditServiceDep,
) -> OrderLifecycleService:
    return OrderLifecycleService(
        OrderRepository(db), ProductRepository(db), PaymentRepository(db), RefundRepository(db),
        ReconciliationRepository(db), provider, audit=audit, job_queue=JobQueue(db), commit=db.commit,
        disputes=DisputeRepository(db), provider_resolver=ProviderResolver(db),
    )


LifecycleServiceDep = Annotated[OrderLifecycleService, Depends(get_lifecycle_service)]
