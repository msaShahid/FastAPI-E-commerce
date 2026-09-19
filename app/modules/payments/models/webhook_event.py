from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.shared.mixins import TimestampMixin


class ProcessedWebhookEvent(Base, TimestampMixin):
    """
    Records every webhook event ID we've successfully processed.
    Stripe's own documentation guarantees AT-LEAST-ONCE delivery -- the
    same event can legitimately arrive twice. Without this table, a
    duplicate delivery of "payment_intent.succeeded" could mark an
    order PAID a second time, or attempt an invalid PAID->PAID status
    transition. The event_id's UNIQUE (primary key) constraint is what
    actually enforces "process each event exactly once," even under
    concurrent duplicate deliveries -- same defense-in-depth pattern as
    every other unique constraint in this project.
    """

    __tablename__ = "processed_webhook_events"

    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)