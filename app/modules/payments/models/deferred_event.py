import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.shared.mixins import TimestampMixin


class DeferredWebhookEvent(Base, TimestampMixin):
    """A verified webhook event about a payment we did not (yet) have a record of, kept for BOUNDED recovery.

    Only the normalised identifiers needed to re-apply the event are stored (ids, amount, currency, status): never the raw
    payload, headers, or any card/customer data. The worker retries it while the window is open and attempts remain; when the
    payment record appears the event is applied through the normal handler (so a late payment for a cancelled order is
    still just reconciled, never fulfilled). When the window or attempts run out it becomes `expired` and the reconciliation
    record opened when it was first seen stays for a human.
    """

    __tablename__ = "deferred_webhook_events"
    __table_args__ = (
        UniqueConstraint("provider", "event_id", name="uq_deferred_events_provider_event"),
        CheckConstraint("status IN ('pending', 'recovered', 'expired')", name="ck_deferred_events_status"),
        Index("ix_deferred_events_due", "status", "next_attempt_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(30))
    event_id: Mapped[str] = mapped_column(String(255))
    event_type: Mapped[str] = mapped_column(String(100))
    provider_payment_id: Mapped[str] = mapped_column(String(255))
    amount: Mapped[int | None] = mapped_column(Integer, default=None)
    amount_received: Mapped[int | None] = mapped_column(Integer, default=None)
    currency: Mapped[str | None] = mapped_column(String(3), default=None)
    provider_refund_id: Mapped[str | None] = mapped_column(String(255), default=None)
    refund_status: Mapped[str | None] = mapped_column(String(50), default=None)
    provider_dispute_id: Mapped[str | None] = mapped_column(String(255), default=None)
    dispute_status: Mapped[str | None] = mapped_column(String(50), default=None)
    dispute_reason: Mapped[str | None] = mapped_column(String(100), default=None)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_reason: Mapped[str | None] = mapped_column(Text, default=None)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
