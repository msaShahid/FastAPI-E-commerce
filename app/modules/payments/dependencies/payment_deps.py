from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from app.core.config import get_settings
from app.core.database import DbSession
from app.core.payments.interface import PaymentProvider
from app.core.payments.stripe_provider import StripePaymentProvider
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
) -> PaymentService:
    return PaymentService(repository, order_service, provider)


PaymentServiceDep = Annotated[PaymentService, Depends(get_payment_service)]
