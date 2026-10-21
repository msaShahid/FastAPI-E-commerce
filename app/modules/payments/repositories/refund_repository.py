from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.payments.models.dispute import Dispute
from app.modules.payments.models.refund import PaymentReconciliation, Refund
from app.shared.enums.lifecycle import ACTIVE_REFUND_STATUSES


class RefundRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, refund_id: UUID, *, for_update: bool = False) -> Refund | None:
        query = select(Refund).where(Refund.id == refund_id)
        if for_update:
            query = query.with_for_update().execution_options(populate_existing=True)
        return (await self.db.execute(query)).scalar_one_or_none()

    async def get_by_provider_refund_id(self, provider_refund_id: str) -> Refund | None:
        return (
            await self.db.execute(select(Refund).where(Refund.provider_refund_id == provider_refund_id))
        ).scalar_one_or_none()

    async def get_active_for_payment(self, payment_id: UUID, *, for_update: bool = False) -> Refund | None:
        """The refund WE started that is still in flight (requested/pending/uncertain), if any."""
        query = select(Refund).where(
            Refund.payment_id == payment_id,
            Refund.origin == "requested",
            Refund.status.in_(["requested", "pending", "uncertain"]),
        )
        if for_update:
            query = query.with_for_update().execution_options(populate_existing=True)
        return (await self.db.execute(query)).scalar_one_or_none()

    async def has_cancelling_refund(self, order_id: UUID) -> bool:
        """A cancel-with-refund for this order that is still in flight."""
        return (
            await self.db.execute(
                select(func.count()).select_from(Refund).where(
                    Refund.order_id == order_id,
                    Refund.cancels_order.is_(True),
                    Refund.status.in_(["requested", "pending", "uncertain"]),
                )
            )
        ).scalar_one() > 0

    async def committed_amount(self, payment_id: UUID) -> int:
        """Sum of the payment's refunds that are in flight or confirmed (what the cumulative limit counts)."""
        return (
            await self.db.execute(
                select(func.coalesce(func.sum(Refund.amount_cents), 0)).where(
                    Refund.payment_id == payment_id, Refund.status.in_([s.value for s in ACTIVE_REFUND_STATUSES])
                )
            )
        ).scalar_one()

    async def count_for_payment(self, payment_id: UUID) -> int:
        return (
            await self.db.execute(select(func.count()).select_from(Refund).where(Refund.payment_id == payment_id))
        ).scalar_one()

    async def create(self, **fields) -> Refund:
        refund = Refund(**fields)
        self.db.add(refund)
        await self.db.flush()
        return refund

    async def save(self, refund: Refund) -> Refund:
        await self.db.flush()
        return refund

    async def list(self, *, status: str | None, offset: int, limit: int):
        conditions = [Refund.status == status] if status else []
        total = (await self.db.execute(select(func.count()).select_from(Refund).where(*conditions))).scalar_one()
        rows = (
            await self.db.execute(
                select(Refund).where(*conditions).order_by(Refund.created_at.desc()).offset(offset).limit(limit)
            )
        ).scalars().all()
        return list(rows), total


class ReconciliationRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def record(self, *, kind: str, dedupe_key: str, payment_id=None, order_id=None,
                     provider_payment_id: str | None = None, details: dict | None = None) -> bool:
        """Records the problem once. Returns False if the same problem (dedupe_key) was already recorded."""
        from sqlalchemy.dialects.postgresql import insert

        statement = (
            insert(PaymentReconciliation)
            .values(
                kind=kind,
                dedupe_key=dedupe_key,
                payment_id=payment_id,
                order_id=order_id,
                provider_payment_id=provider_payment_id,
                details=details or {},
            )
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
            .returning(PaymentReconciliation.id)
        )
        return (await self.db.execute(statement)).scalar_one_or_none() is not None

    async def get(self, record_id: UUID, *, for_update: bool = False) -> PaymentReconciliation | None:
        query = select(PaymentReconciliation).where(PaymentReconciliation.id == record_id)
        if for_update:
            query = query.with_for_update().execution_options(populate_existing=True)
        return (await self.db.execute(query)).scalar_one_or_none()

    async def list(self, *, status: str | None, kind: str | None, offset: int, limit: int):
        conditions = []
        if status:
            conditions.append(PaymentReconciliation.status == status)
        if kind:
            conditions.append(PaymentReconciliation.kind == kind)
        total = (
            await self.db.execute(select(func.count()).select_from(PaymentReconciliation).where(*conditions))
        ).scalar_one()
        rows = (
            await self.db.execute(
                select(PaymentReconciliation).where(*conditions)
                .order_by(PaymentReconciliation.created_at.desc()).offset(offset).limit(limit)
            )
        ).scalars().all()
        return list(rows), total

    async def resolve_by_dedupe_key(self, dedupe_key: str, note: str) -> bool:
        """Closes an OPEN record the system recovered by itself (no human actor), with a note saying so."""
        from datetime import UTC, datetime

        from sqlalchemy import update

        result = await self.db.execute(
            update(PaymentReconciliation)
            .where(PaymentReconciliation.dedupe_key == dedupe_key, PaymentReconciliation.status == "open")
            .values(status="resolved", resolution="no_action_needed", resolution_note=note,
                    resolved_at=datetime.now(UTC).replace(tzinfo=None))
        )
        return result.rowcount > 0

    async def count_open(self) -> int:
        return (
            await self.db.execute(
                select(func.count()).select_from(PaymentReconciliation).where(PaymentReconciliation.status == "open")
            )
        ).scalar_one()

    async def save(self, record: PaymentReconciliation) -> PaymentReconciliation:
        await self.db.flush()
        return record


class DisputeRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_provider_id(self, provider: str, provider_dispute_id: str, *, for_update: bool = False):
        query = select(Dispute).where(Dispute.provider == provider, Dispute.provider_dispute_id == provider_dispute_id)
        if for_update:
            query = query.with_for_update().execution_options(populate_existing=True)
        return (await self.db.execute(query)).scalar_one_or_none()

    async def create(self, **fields) -> Dispute:
        dispute = Dispute(**fields)
        self.db.add(dispute)
        await self.db.flush()
        return dispute

    async def save(self, dispute: Dispute) -> Dispute:
        await self.db.flush()
        return dispute

    async def has_open(self, payment_id: UUID) -> bool:
        return (
            await self.db.execute(
                select(func.count()).select_from(Dispute).where(
                    Dispute.payment_id == payment_id, Dispute.status.in_(["open", "under_review"])
                )
            )
        ).scalar_one() > 0

    async def list(self, *, status: str | None, offset: int, limit: int):
        conditions = [Dispute.status == status] if status else []
        total = (await self.db.execute(select(func.count()).select_from(Dispute).where(*conditions))).scalar_one()
        rows = (
            await self.db.execute(
                select(Dispute).where(*conditions).order_by(Dispute.created_at.desc()).offset(offset).limit(limit)
            )
        ).scalars().all()
        return list(rows), total
