from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.payments.models.deferred_event import DeferredWebhookEvent
from app.modules.payments.models.payment import Payment
from app.modules.payments.models.webhook_event import ProcessedWebhookEvent
from app.shared.enums.payment_status import PaymentStatus


class PaymentRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, payment_id: UUID) -> Payment | None:
        return await self.db.get(Payment, payment_id)

    async def get_by_provider_payment_id(
        self, provider_payment_id: str, provider: str | None = None
    ) -> Payment | None:
        """With `provider`, only THAT provider's payment is found: an event from one provider can never touch another's."""
        query = select(Payment).where(Payment.provider_payment_id == provider_payment_id)
        if provider is not None:
            query = query.where(Payment.provider == provider)
        return (await self.db.execute(query)).scalar_one_or_none()

    async def get_by_provider_reference(self, reference: str, provider: str) -> Payment | None:
        return (await self.db.execute(select(Payment).where(
            Payment.provider_reference == reference, Payment.provider == provider))).scalar_one_or_none()

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
        provider_config_id=None,
    ) -> Payment:
        payment = Payment(
            order_id=order_id,
            provider=provider,
            provider_config_id=provider_config_id,
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

    async def defer_event(self, event, *, window_minutes: int, interval_seconds: int, reason: str) -> bool:
        """Keeps a verified event about an UNKNOWN payment for bounded recovery. Idempotent per (provider, event id).
        Stores identifiers only: never the payload. Returns whether a new record was created."""
        from datetime import timedelta

        now = datetime.now(UTC)
        stmt = (
            pg_insert(DeferredWebhookEvent)
            .values(
                provider=event.provider, event_id=event.event_id, event_type=event.event_type,
                provider_payment_id=event.provider_payment_id, amount=event.amount, amount_received=event.amount_received,
                currency=event.currency, provider_refund_id=event.provider_refund_id, refund_status=event.refund_status,
                provider_dispute_id=event.provider_dispute_id, dispute_status=event.dispute_status,
                dispute_reason=event.dispute_reason, status="pending", attempts=0,
                next_attempt_at=now + timedelta(seconds=max(interval_seconds, 1)),
                expires_at=now + timedelta(minutes=window_minutes), last_reason=reason[:300],
            )
            .on_conflict_do_nothing(constraint="uq_deferred_events_provider_event")
            .returning(DeferredWebhookEvent.id)
        )
        created = (await self.db.execute(stmt)).scalar_one_or_none() is not None
        await self.db.flush()
        return created

    async def claim_due_deferred(self, *, limit: int):
        """Due, still-pending events, locked so concurrent workers never take the same one (SKIP LOCKED)."""
        rows = await self.db.execute(
            select(DeferredWebhookEvent)
            .where(DeferredWebhookEvent.status == "pending", DeferredWebhookEvent.next_attempt_at <= datetime.now(UTC))
            .order_by(DeferredWebhookEvent.next_attempt_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        return list(rows.scalars().all())

    async def list_deferred(self, *, status: str | None, offset: int, limit: int):
        conditions = [DeferredWebhookEvent.status == status] if status else []
        total = (await self.db.execute(select(func.count()).select_from(DeferredWebhookEvent).where(*conditions))).scalar_one()
        rows = (
            await self.db.execute(
                select(DeferredWebhookEvent).where(*conditions)
                .order_by(DeferredWebhookEvent.created_at.desc()).offset(offset).limit(limit)
            )
        ).scalars().all()
        return list(rows), total
