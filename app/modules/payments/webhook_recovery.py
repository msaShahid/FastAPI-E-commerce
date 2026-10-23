"""Recording a failed webhook attempt. The failed attempt's own transaction is rolled back (so none of its side
effects persist); this writes a tiny separate record so the failure is visible and the provider's retry is
recognised as a retry, not a duplicate. Only the exception type and message are stored (never a payload)."""

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.redaction import redact
from app.modules.payments.models.webhook_event import ProcessedWebhookEvent


async def record_failed_event(session_factory, event, error: Exception) -> None:
    message = redact(f"{type(error).__name__}: {error}")[:500]
    async with session_factory() as session, session.begin():
        await session.execute(
            pg_insert(ProcessedWebhookEvent)
            .values(provider=event.provider, event_id=event.event_id, event_type=event.event_type,
                    status="failed", attempts=1, last_error=message)
            .on_conflict_do_update(
                index_elements=["provider", "event_id"],
                set_={"status": "failed", "attempts": ProcessedWebhookEvent.attempts + 1, "last_error": message},
                where=ProcessedWebhookEvent.status == "failed",  # never overwrite a processed/ignored record
            )
        )
