from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.email_settings.models.email_provider_config import EmailProviderConfig


class EmailProviderRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, provider: str) -> EmailProviderConfig | None:
        return (await self.db.execute(select(EmailProviderConfig).where(EmailProviderConfig.provider == provider))).scalar_one_or_none()

    async def get_active(self) -> EmailProviderConfig | None:
        return (await self.db.execute(select(EmailProviderConfig).where(EmailProviderConfig.is_active.is_(True)))).scalar_one_or_none()

    async def list_all(self) -> list[EmailProviderConfig]:
        return list((await self.db.execute(select(EmailProviderConfig).order_by(EmailProviderConfig.provider))).scalars().all())

    async def add(self, row: EmailProviderConfig) -> EmailProviderConfig:
        self.db.add(row)
        await self.db.flush()
        return row

    async def flush(self) -> None:
        await self.db.flush()

    async def deactivate_all(self) -> None:
        await self.db.execute(update(EmailProviderConfig).where(EmailProviderConfig.is_active.is_(True)).values(is_active=False))
        await self.db.flush()

    async def delete(self, row: EmailProviderConfig) -> None:
        await self.db.execute(delete(EmailProviderConfig).where(EmailProviderConfig.id == row.id))
        await self.db.flush()

    async def record_send(self, config_id, error_code: str | None) -> None:
        now = datetime.now(UTC)
        values = {"last_sent_at": now} if error_code is None else {"last_error_at": now, "last_error_code": error_code}
        if error_code is None:
            values["last_error_code"] = None  # a success clears the previous failure
        await self.db.execute(update(EmailProviderConfig).where(EmailProviderConfig.id == config_id).values(**values))
