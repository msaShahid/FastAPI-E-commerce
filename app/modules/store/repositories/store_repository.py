from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.orders.models.order import Order
from app.modules.store.models.store_settings import StoreSettings


class StoreRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_currency(self) -> str | None:
        return (await self.db.execute(select(StoreSettings.currency).where(StoreSettings.id == 1))).scalar_one_or_none()

    async def any_orders(self) -> bool:
        return (await self.db.execute(select(func.count()).select_from(Order))).scalar_one() > 0

    async def initialize_currency(self, code: str) -> bool:
        """Records the store currency if none is recorded yet. True if THIS call initialised it; False if a currency was
        already recorded (including by a concurrent request). It never changes an existing value."""
        result = await self.db.execute(
            pg_insert(StoreSettings).values(id=1, currency=code).on_conflict_do_nothing(index_elements=["id"]).returning(StoreSettings.id)
        )
        await self.db.flush()
        return result.scalar_one_or_none() is not None

    async def effective_currency(self, default: str) -> tuple[str | None, bool]:
        """(currency, configured). A configured store uses its currency. A NEW store (nothing configured, no orders
        yet) uses the default. A store that already has orders but no configured currency gets NO default: an
        administrator must choose, because assuming one could silently misstate history."""
        code = await self.get_currency()
        if code is not None:
            return code, True
        if await self.any_orders():
            return None, False
        return default, False

    async def get_or_initialize_currency(self, default: str) -> str | None:
        """Like effective_currency, but PERSISTS the default for a new store, so it stays fixed from then on even if
        the configured default later changes. Safe under concurrent first requests."""
        code, configured = await self.effective_currency(default)
        if code is None or configured:
            return code
        await self.db.execute(
            pg_insert(StoreSettings).values(id=1, currency=code).on_conflict_do_nothing(index_elements=["id"])
        )
        await self.db.flush()
        return await self.get_currency() or code
