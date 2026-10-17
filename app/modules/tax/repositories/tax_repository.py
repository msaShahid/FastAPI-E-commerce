from datetime import date
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.tax.models.tax import GstRegistration, TaxClassification, TaxRule


class TaxRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, model: Any, record_id: int, *, for_update: bool = False):
        query = select(model).where(model.id == record_id)
        if for_update:
            query = query.with_for_update().execution_options(populate_existing=True)
        return (await self.db.execute(query)).scalar_one_or_none()

    async def add(self, record):
        self.db.add(record)
        await self.db.flush()
        return record

    async def save(self, record):
        await self.db.flush()
        return record

    async def list(self, model: Any, conditions: list, *, offset: int, limit: int):
        total = (
            await self.db.execute(select(func.count()).select_from(model).where(*conditions))
        ).scalar_one()
        rows = (
            await self.db.execute(
                select(model).where(*conditions).order_by(model.id.desc()).offset(offset).limit(limit)
            )
        ).scalars().all()
        return list(rows), total

    async def classification_by_kind_code(self, kind: str, code: str):
        return (
            await self.db.execute(
                select(TaxClassification).where(
                    TaxClassification.kind == kind, TaxClassification.code == code
                )
            )
        ).scalar_one_or_none()

    async def registration_by_gstin(self, gstin: str):
        return (
            await self.db.execute(select(GstRegistration).where(GstRegistration.gstin == gstin))
        ).scalar_one_or_none()

    async def has_open_rules(self, classification_id: int) -> bool:
        count = (
            await self.db.execute(
                select(func.count())
                .select_from(TaxRule)
                .where(TaxRule.classification_id == classification_id, TaxRule.status != "retired")
            )
        ).scalar_one()
        return count > 0

    async def overlapping_approved_rule(
        self, classification_id: int, start: date, end: date | None, exclude_id: int
    ):
        """Approved rules whose [from, to) range intersects [start, end). NULL end = open-ended."""
        conditions = [
            TaxRule.classification_id == classification_id,
            TaxRule.status == "approved",
            TaxRule.id != exclude_id,
            or_(TaxRule.effective_to.is_(None), TaxRule.effective_to > start),
        ]
        if end is not None:
            conditions.append(TaxRule.effective_from < end)
        return (await self.db.execute(select(TaxRule.id).where(*conditions).limit(1))).first()
