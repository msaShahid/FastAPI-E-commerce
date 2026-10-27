from fastapi import APIRouter

from app.modules.auth.dependencies.auth import AdminUser
from app.modules.store.dependencies.store_deps import StoreServiceDep
from app.modules.store.schemas.store import (
    StoreCurrencyRead,
    StoreCurrencyUpdate,
    StoreSettingsRead,
)

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


@store_router.put("/currency", response_model=StoreCurrencyRead)
async def set_store_currency(
    payload: StoreCurrencyUpdate, admin: AdminUser, service: StoreServiceDep
) -> StoreCurrencyRead:
    """Administrator: choose the store currency during initial setup. It can be set ONCE; afterwards it is fixed for the
    lifetime of the store (repeating the same value is accepted, any other value is refused)."""
    return await service.set_currency(admin, payload.currency)
