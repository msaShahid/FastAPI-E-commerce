from sqlalchemy import func, select
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

    async def set_currency(self, code: str) -> None:
        row = (await self.db.execute(select(StoreSettings).where(StoreSettings.id == 1))).scalar_one_or_none()
        if row is None:
            self.db.add(StoreSettings(id=1, currency=code))
        else:
            row.currency = code
        await self.db.flush()
