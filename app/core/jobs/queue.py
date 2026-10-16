from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.jobs.models import BackgroundJob


class JobQueue:
    """
    Puts jobs on the queue using the CALLER'S session.

    Nothing is committed here. The job becomes visible to the worker when the
    request's own transaction commits, and disappears if it rolls back. That
    is the point: "the order was marked paid" and "the confirmation email is
    queued" either both happen or neither does, with no window in between.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def enqueue(
        self,
        job_type: str,
        payload: dict | None = None,
        *,
        dedupe_key: str | None = None,
        delay_seconds: float = 0,
        max_attempts: int | None = None,
    ) -> int | None:
        """
        Returns the new job's id, or None if a job with this dedupe_key
        already exists (nothing is added in that case).
        """
        statement = (
            insert(BackgroundJob)
            .values(
                job_type=job_type,
                payload=payload or {},
                max_attempts=max_attempts or get_settings().jobs_default_max_attempts,
                run_at=func.now() + timedelta(seconds=delay_seconds),
                dedupe_key=dedupe_key,
            )
            .on_conflict_do_nothing(index_elements=["dedupe_key"])
            .returning(BackgroundJob.id)
        )
        return (await self.db.execute(statement)).scalar_one_or_none()
