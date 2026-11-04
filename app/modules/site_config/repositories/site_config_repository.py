import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.site_config.models.site_config import SiteConfigState, SiteConfigVersion


class SiteConfigRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def state(self, *, lock: bool = False) -> SiteConfigState:
        """The single state row, created on first use. `lock` holds it until the transaction ends (publishing takes turns)."""
        query = select(SiteConfigState).where(SiteConfigState.id == 1)
        if lock:
            query = query.with_for_update()
        row = (await self.db.execute(query)).scalar_one_or_none()
        if row is None:
            row = SiteConfigState(id=1)
            self.db.add(row)
            await self.db.flush()
            if lock:
                row = (await self.db.execute(query)).scalar_one()
        return row

    async def get_version(self, version: int) -> SiteConfigVersion | None:
        return (await self.db.execute(select(SiteConfigVersion).where(SiteConfigVersion.version == version))).scalar_one_or_none()

    async def add_version(self, *, document: dict, note: str | None, user_id: uuid.UUID | None) -> SiteConfigVersion:
        latest = (await self.db.execute(select(func.coalesce(func.max(SiteConfigVersion.version), 0)))).scalar_one()
        row = SiteConfigVersion(version=latest + 1, document=document, note=note, created_by_user_id=user_id)
        self.db.add(row)
        await self.db.flush()
        return row

    async def list_versions(self, *, offset: int, limit: int) -> tuple[list[SiteConfigVersion], int]:
        total = (await self.db.execute(select(func.count()).select_from(SiteConfigVersion))).scalar_one()
        rows = (await self.db.execute(select(SiteConfigVersion).order_by(SiteConfigVersion.version.desc()).offset(offset).limit(limit))).scalars().all()
        return list(rows), total

    async def save_draft(self, state: SiteConfigState, *, draft: dict | None, user_id: uuid.UUID | None, now: datetime) -> None:
        state.draft, state.draft_updated_at, state.draft_updated_by_user_id = draft, (now if draft is not None else None), user_id
        await self.db.flush()
