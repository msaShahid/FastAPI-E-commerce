from datetime import date
from typing import Any

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.products.models.product import Product
from app.modules.tax.models.tax import (
    BusinessTaxSettings,
    GstRegistration,
    TaxClassification,
    TaxRule,
)
from app.shared.enums.product_status import ProductStatus


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

    async def get_registration(self, registration_id: int):
        return await self.get(GstRegistration, registration_id)

    async def classification_statuses(self, ids) -> dict[int, str]:
        ids = list(ids)
        if not ids:
            return {}
        rows = await self.db.execute(
            select(TaxClassification.id, TaxClassification.status).where(TaxClassification.id.in_(ids))
        )
        return {cid: status for cid, status in rows.all()}

    async def classifications_by_ids(self, ids) -> "dict[int, TaxClassification]":  # noqa: UP037
        ids = list(ids)
        if not ids:
            return {}
        rows = await self.db.execute(select(TaxClassification).where(TaxClassification.id.in_(ids)))
        return {c.id: c for c in rows.scalars().all()}

    async def approved_rules_covering(self, ids, as_of: date) -> "dict[int, list[TaxRule]]":  # noqa: UP037
        """Approved rules per classification whose [from, to) range contains `as_of`."""
        ids = list(ids)
        if not ids:
            return {}
        rows = await self.db.execute(
            select(TaxRule).where(
                TaxRule.classification_id.in_(ids),
                TaxRule.status == "approved",
                TaxRule.effective_from <= as_of,
                or_(TaxRule.effective_to.is_(None), TaxRule.effective_to > as_of),
            )
        )
        found: dict[int, list[TaxRule]] = {}
        for rule in rows.scalars().all():
            found.setdefault(rule.classification_id, []).append(rule)
        return found

    async def approved_settings_covering(self, as_of: date) -> "list[BusinessTaxSettings]":  # noqa: UP037
        rows = await self.db.execute(
            select(BusinessTaxSettings).where(
                BusinessTaxSettings.status == "approved",
                BusinessTaxSettings.effective_from <= as_of,
                or_(BusinessTaxSettings.effective_to.is_(None), BusinessTaxSettings.effective_to > as_of),
            )
        )
        return list(rows.scalars().all())

    async def overlapping_approved_settings(self, start: date, end: date | None, exclude_id: int):
        conditions = [
            BusinessTaxSettings.status == "approved",
            BusinessTaxSettings.id != exclude_id,
            or_(BusinessTaxSettings.effective_to.is_(None), BusinessTaxSettings.effective_to > start),
        ]
        if end is not None:
            conditions.append(BusinessTaxSettings.effective_from < end)
        return (await self.db.execute(select(BusinessTaxSettings.id).where(*conditions).limit(1))).first()

    async def registration_has_open_settings(self, registration_id: int) -> bool:
        count = (
            await self.db.execute(
                select(func.count())
                .select_from(BusinessTaxSettings)
                .where(
                    BusinessTaxSettings.registration_id == registration_id,
                    BusinessTaxSettings.status != "retired",
                )
            )
        ).scalar_one()
        return count > 0

    async def readiness_counts(self, as_of: date) -> dict[str, int]:
        """How many ACTIVE products cannot be taxed yet, and why. Read-only."""
        active = Product.status == ProductStatus.ACTIVE
        covering = exists().where(
            TaxRule.classification_id == Product.tax_classification_id,
            TaxRule.status == "approved",
            TaxRule.effective_from <= as_of,
            or_(TaxRule.effective_to.is_(None), TaxRule.effective_to > as_of),
        )

        async def count(*conditions, join=False) -> int:
            query = select(func.count()).select_from(Product)
            if join:
                query = query.join(TaxClassification, Product.tax_classification_id == TaxClassification.id)
            return (await self.db.execute(query.where(active, *conditions))).scalar_one()

        return {
            "active_products": await count(),
            "active_unclassified": await count(Product.tax_classification_id.is_(None)),
            "active_classification_not_approved": await count(TaxClassification.status != "approved", join=True),
            "active_without_effective_rule": await count(TaxClassification.status == "approved", ~covering, join=True),
        }

    async def count_products_using(self, classification_id: int) -> int:
        return (
            await self.db.execute(
                select(func.count())
                .select_from(Product)
                .where(Product.tax_classification_id == classification_id)
            )
        ).scalar_one()

    async def list_products_by_tax_state(self, state, *, offset: int, limit: int):
        """(product, classification status | None) rows, filtered by assignment state."""
        conditions = []
        if state == "unassigned":
            conditions.append(Product.tax_classification_id.is_(None))
        elif state == "assigned_approved":
            conditions.append(TaxClassification.status == "approved")
        elif state == "assigned_unapproved":
            conditions.append(Product.tax_classification_id.is_not(None))
            conditions.append(TaxClassification.status != "approved")
        base = select(Product, TaxClassification.status).outerjoin(
            TaxClassification, Product.tax_classification_id == TaxClassification.id
        )
        total = (
            await self.db.execute(
                select(func.count())
                .select_from(Product)
                .outerjoin(TaxClassification, Product.tax_classification_id == TaxClassification.id)
                .where(*conditions)
            )
        ).scalar_one()
        rows = (
            await self.db.execute(
                base.where(*conditions).order_by(Product.id).offset(offset).limit(limit)
            )
        ).all()
        return rows, total

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
