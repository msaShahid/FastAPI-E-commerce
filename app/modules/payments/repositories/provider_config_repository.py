from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.payments.models.demo_intent import DemoPaymentIntent  # noqa: F401  (registers the table with the schema)
from app.modules.payments.models.payment_settings import PaymentSettings
from app.modules.payments.models.provider_config import PaymentProviderConfig


class ProviderConfigRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, config_id: UUID, *, for_update: bool = False) -> PaymentProviderConfig | None:
        query = select(PaymentProviderConfig).where(PaymentProviderConfig.id == config_id)
        if for_update:
            query = query.with_for_update().execution_options(populate_existing=True)
        return (await self.db.execute(query)).scalar_one_or_none()

    async def get_by_provider_mode(self, provider: str, mode: str) -> PaymentProviderConfig | None:
        return (await self.db.execute(select(PaymentProviderConfig).where(
            PaymentProviderConfig.provider == provider, PaymentProviderConfig.mode == mode))).scalar_one_or_none()

    async def list_all(self) -> list[PaymentProviderConfig]:
        rows = await self.db.execute(select(PaymentProviderConfig).order_by(PaymentProviderConfig.provider, PaymentProviderConfig.mode))
        return list(rows.scalars().all())

    async def list_for_provider(self, provider: str) -> list[PaymentProviderConfig]:
        rows = await self.db.execute(select(PaymentProviderConfig).where(PaymentProviderConfig.provider == provider))
        return list(rows.scalars().all())

    async def any_exists(self) -> bool:
        return (await self.db.execute(select(func.count()).select_from(PaymentProviderConfig))).scalar_one() > 0

    async def get_preferred(self) -> PaymentProviderConfig | None:
        return (await self.db.execute(select(PaymentProviderConfig).where(PaymentProviderConfig.is_preferred.is_(True)))).scalar_one_or_none()

    async def create(self, **fields) -> PaymentProviderConfig:
        config = PaymentProviderConfig(**fields)
        self.db.add(config)
        await self.db.flush()
        return config

    async def save(self, config: PaymentProviderConfig) -> PaymentProviderConfig:
        await self.db.flush()
        return config

    async def clear_preferred(self) -> None:
        await self.db.execute(update(PaymentProviderConfig).where(PaymentProviderConfig.is_preferred.is_(True)).values(is_preferred=False))
        await self.db.flush()


class PaymentSettingsRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, *, for_update: bool = False) -> PaymentSettings:
        """The single settings row (created on first use, with live payments OFF)."""
        await self.db.execute(pg_insert(PaymentSettings).values(id=1).on_conflict_do_nothing(index_elements=[PaymentSettings.id]))
        query = select(PaymentSettings).where(PaymentSettings.id == 1)
        if for_update:
            query = query.with_for_update().execution_options(populate_existing=True)
        return (await self.db.execute(query)).scalar_one()

    async def live_active(self) -> bool:
        return bool((await self.db.execute(select(PaymentSettings.live_payments_active).where(PaymentSettings.id == 1))).scalar_one_or_none())

