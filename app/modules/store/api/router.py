from fastapi import APIRouter

from app.modules.roles.dependencies.permission_deps import StoreConfigure
from app.modules.store.dependencies.store_deps import StoreServiceDep
from app.modules.store.schemas.store import (
    PaymentConfigRead,
    StateRead,
    StoreCurrencyRead,
    StoreCurrencyUpdate,
    StoreSettingsRead,
)
from app.modules.tax.gst_states import STATES

store_router = APIRouter(prefix="/store", tags=["store"])


@store_router.get("/currency", response_model=StoreCurrencyRead)
async def get_store_currency(service: StoreServiceDep) -> StoreCurrencyRead:
    """Public: the currency all prices are in (and its minor-unit exponent), for formatting."""
    return await service.read_currency()


@store_router.get("/settings", response_model=StoreSettingsRead)
async def get_store_settings(service: StoreServiceDep) -> StoreSettingsRead:
    """Public: how buying works in this store (currency, shipping, free-shipping threshold, unpaid-order window) and whether
    checkout and payments are currently possible, so a storefront can say so instead of failing."""
    return await service.read_public_settings()


@store_router.get("/payment-config", response_model=PaymentConfigRead)
async def get_payment_config(service: StoreServiceDep) -> PaymentConfigRead:
    """Public: which gateway a new payment would use right now and the PUBLIC key a browser needs for it (a Stripe publishable key or a
    Razorpay key id). `browser_ready` is false while that key is missing. Never returns a secret."""
    return await service.read_payment_config()


@store_router.get("/states", response_model=list[StateRead])
async def list_states() -> list[StateRead]:
    """Public: the Indian states and union territories (with their two-digit GST codes) for the delivery-address form. GST is worked out
    from the delivery state, so a storefront should offer exactly these instead of free text."""
    return [StateRead(code=s.code, name=s.name) for s in STATES]


@store_router.put("/currency", response_model=StoreCurrencyRead)
async def set_store_currency(
    payload: StoreCurrencyUpdate, admin: StoreConfigure, service: StoreServiceDep
) -> StoreCurrencyRead:
    """Administrator: choose the store currency during initial setup. It can be set ONCE; afterwards it is fixed for the
    lifetime of the store (repeating the same value is accepted, any other value is refused)."""
    return await service.set_currency(admin, payload.currency)
