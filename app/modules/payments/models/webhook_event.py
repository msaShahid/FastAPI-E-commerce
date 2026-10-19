from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.shared.mixins import TimestampMixin


class ProcessedWebhookEvent(Base, TimestampMixin):
    """Every webhook event we accepted (signature verified), scoped per provider, with its outcome.

    processed = handled; ignored = a type we do not act on (kept so it can be investigated, never silently
    dropped); failed = handling raised, rolled back, and will be retried by the provider (attempts counts tries).
    No payload is stored: only identifiers and the outcome, so no payment data or secret lands here.
    """

    __tablename__ = "processed_webhook_events"
    __table_args__ = (
        CheckConstraint("status IN ('processing', 'processed', 'ignored', 'failed')", name="ck_webhook_events_status"),
    )

    provider: Mapped[str] = mapped_column(String(30), primary_key=True, default="stripe", server_default="stripe")
    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_type: Mapped[str | None] = mapped_column(String(100), default=None)
    status: Mapped[str] = mapped_column(String(20), default="processed", server_default="processed")
    attempts: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
