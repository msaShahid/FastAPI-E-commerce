from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.payments.models.payment import Payment
from app.modules.payments.models.webhook_event import ProcessedWebhookEvent
from app.shared.enums.payment_status import PaymentStatus


class PaymentRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, payment_id: UUID) -> Payment | None:
        return await self.db.get(Payment, payment_id)

    async def get_by_provider_payment_id(
        self, provider_payment_id: str
    ) -> Payment | None:
        result = await self.db.execute(
            select(Payment).where(Payment.provider_payment_id == provider_payment_id)
        )
        return result.scalar_one_or_none()

    async def list_for_order(self, order_id: UUID) -> list[Payment]:
        result = await self.db.execute(
            select(Payment)
            .where(Payment.order_id == order_id)
            .order_by(Payment.created_at.desc())
        )
        return list(result.scalars().all())

    async def create(
        self,
        *,
        order_id: UUID,
        provider_payment_id: str,
        amount_cents: int,
        currency: str = "usd",
        provider: str = "stripe",
    ) -> Payment:
        payment = Payment(
            order_id=order_id,
            provider=provider,
            provider_payment_id=provider_payment_id,
            amount_cents=amount_cents,
            currency=currency,
            status=PaymentStatus.PENDING,
        )
        self.db.add(payment)
        await self.db.flush()
        return payment

    async def update_status(self, payment: Payment, status: PaymentStatus) -> Payment:
        payment.status = status
        await self.db.flush()
        return payment

    async def mark_refunded(
        self, payment: Payment, *, provider_refund_id: str, refunded_amount_cents: int
    ) -> Payment:
        payment.status = PaymentStatus.REFUNDED
        payment.provider_refund_id = provider_refund_id
        payment.refunded_amount_cents = refunded_amount_cents
        await self.db.flush()
        return payment

    async def get_processed_event(self, event_id: str, provider: str = "stripe") -> ProcessedWebhookEvent | None:
        return await self.db.get(ProcessedWebhookEvent, (provider, event_id))

    async def claim_webhook_event(self, event) -> bool:
        """Claims an event for processing. True for a NEW event, or one whose previous attempt FAILED (a retry);
        False for a duplicate of one already processed/ignored/in progress. Atomic: a concurrent duplicate
        blocks on the key until the first delivery's transaction ends, then sees it taken."""
        stmt = (
            pg_insert(ProcessedWebhookEvent)
            .values(provider=event.provider, event_id=event.event_id, event_type=event.event_type, status="processing")
            .on_conflict_do_update(
                index_elements=["provider", "event_id"],
                set_={"status": "processing", "event_type": event.event_type,
                      "attempts": ProcessedWebhookEvent.attempts + 1},
                where=ProcessedWebhookEvent.status == "failed",
            )
            .returning(ProcessedWebhookEvent.event_id)
        )
        result = await self.db.execute(stmt)
        await self.db.flush()
        return result.scalar_one_or_none() is not None

    async def finish_webhook_event(self, event, status: str) -> None:
        row = await self.db.get(ProcessedWebhookEvent, (event.provider, event.event_id))
        if row is not None:
            row.status = status
            row.last_error = None
            row.processed_at = datetime.now(UTC)
            await self.db.flush()

    async def list_webhook_events(self, *, status: str | None, offset: int, limit: int):
        conditions = [ProcessedWebhookEvent.status == status] if status else []
        total = (
            await self.db.execute(select(func.count()).select_from(ProcessedWebhookEvent).where(*conditions))
        ).scalar_one()
        rows = (
            await self.db.execute(
                select(ProcessedWebhookEvent).where(*conditions)
                .order_by(ProcessedWebhookEvent.created_at.desc()).offset(offset).limit(limit)
            )
        ).scalars().all()
        return list(rows), total

    async def add_refunded_amount(self, payment: Payment, *, provider_refund_id: str | None, amount: int) -> Payment:
        """Adds a provider-CONFIRMED refund to the payment's running total. The payment becomes `refunded` only when
        the total reaches its full amount; a partial refund leaves it `succeeded`."""
        payment.refunded_amount_cents = (payment.refunded_amount_cents or 0) + amount
        if provider_refund_id:
            payment.provider_refund_id = provider_refund_id
        if payment.refunded_amount_cents >= payment.amount_cents:
            payment.status = PaymentStatus.REFUNDED
        await self.db.flush()
        return payment
