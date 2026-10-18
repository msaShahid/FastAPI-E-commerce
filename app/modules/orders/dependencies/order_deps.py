from typing import Annotated

from fastapi import Depends

from app.core.config import get_settings
from app.core.database import DbSession
from app.modules.addresses.dependencies.address_deps import get_address_repository
from app.modules.addresses.repositories.address_repository import AddressRepository
from app.modules.cart.dependencies.cart_deps import get_cart_repository
from app.modules.cart.repositories.cart_repository import CartRepository
from app.modules.orders.repositories.order_repository import OrderRepository
from app.modules.orders.services.order_service import OrderService
from app.modules.products.dependencies.product_deps import get_product_repository
from app.modules.products.repositories.product_repository import ProductRepository
from app.modules.tax.engine import GstTaxEngine, NotConfiguredTaxEngine, TaxEngine
from app.modules.tax.repositories.tax_repository import TaxRepository


def get_order_repository(db: DbSession) -> OrderRepository:
    return OrderRepository(db)


def get_tax_engine(db: DbSession) -> TaxEngine:
    """Selects the tax engine from configuration. Overridable in tests.

    GST enabled -> the GST engine (readiness checks, then refuses: not implemented yet).
    Otherwise a configured flat percentage -> the interim engine (OrderService builds it).
    Otherwise -> not configured (checkout refuses).
    """
    settings = get_settings()
    if settings.tax_calculation_enabled:
        return GstTaxEngine(TaxRepository(db))
    return NotConfiguredTaxEngine() if settings.tax_rate_percent is None else _flat(settings)


def _flat(settings) -> TaxEngine:
    from app.modules.tax.engine import FlatInterimTaxEngine

    return FlatInterimTaxEngine(settings.tax_rate_percent)


def get_order_service(
    repository: Annotated[OrderRepository, Depends(get_order_repository)],
    cart_repository: Annotated[CartRepository, Depends(get_cart_repository)],
    product_repository: Annotated[ProductRepository, Depends(get_product_repository)],
    address_repository: Annotated[AddressRepository, Depends(get_address_repository)],
    tax_engine: Annotated[TaxEngine, Depends(get_tax_engine)],
) -> OrderService:
    # Settings are read per request (not at import) and passed explicitly. Unset
    # shipping/tax stay None, which makes checkout refuse instead of charging 0.
    settings = get_settings()
    return OrderService(
        repository,
        cart_repository,
        product_repository,
        address_repository,
        flat_shipping_cents=settings.flat_shipping_cents,
        free_shipping_threshold_cents=settings.free_shipping_threshold_cents,
        free_shipping_threshold_basis=settings.free_shipping_threshold_basis,
        tax_rate_percent=settings.tax_rate_percent,
        require_tax_classification=settings.tax_classification_required,
        tax_engine=tax_engine,
    )


OrderServiceDep = Annotated[OrderService, Depends(get_order_service)]
