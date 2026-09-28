from uuid import UUID

from sqlalchemy import select
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

    async def get_processed_event(self, event_id: str) -> ProcessedWebhookEvent | None:
        return await self.db.get(ProcessedWebhookEvent, event_id)

    async def claim_webhook_event(self, event_id: str) -> bool:
  
        stmt = (
            pg_insert(ProcessedWebhookEvent)
            .values(event_id=event_id)
            .on_conflict_do_nothing(index_elements=["event_id"])
            .returning(ProcessedWebhookEvent.event_id)
        )
        result = await self.db.execute(stmt)
        await self.db.flush()
        return result.scalar_one_or_none() is not None