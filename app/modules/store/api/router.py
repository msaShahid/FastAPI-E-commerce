from fastapi import APIRouter

from app.modules.auth.dependencies.auth import AdminUser
from app.modules.store.dependencies.store_deps import StoreServiceDep
from app.modules.store.schemas.store import StoreCurrencyRead, StoreCurrencyUpdate

store_router = APIRouter(prefix="/store", tags=["store"])


@store_router.get("/currency", response_model=StoreCurrencyRead)
async def get_store_currency(service: StoreServiceDep) -> StoreCurrencyRead:
    """Public: the currency all prices are in (and its minor-unit exponent), for formatting."""
    return await service.read_currency()


@store_router.put("/currency", response_model=StoreCurrencyRead)
async def set_store_currency(
    payload: StoreCurrencyUpdate, admin: AdminUser, service: StoreServiceDep
) -> StoreCurrencyRead:
    """Administrator: set the store currency. Refused once any order exists."""
    return await service.set_currency(admin, payload.currency)
