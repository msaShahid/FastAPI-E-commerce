from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.shared.mixins import TimestampMixin

PENDING = "pending"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"  # out of attempts; needs a human (see docs/background-jobs.md)


class BackgroundJob(Base, TimestampMixin):
    """
    One unit of deferred work.

    All the time logic (run_at, locked_until) is evaluated by Postgres with
    now(), never by the Python process, so workers on machines with slightly
    different clocks still agree on what is due.
    """

    __tablename__ = "background_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_background_jobs_status",
        ),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="ck_background_jobs_attempts"),
        # The worker's "what is due?" query only ever looks at unfinished jobs,
        # so finished ones (the vast majority over time) are kept out of the index.
        Index(
            "ix_background_jobs_claim",
            "run_at",
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))

    status: Mapped[str] = mapped_column(String(20), default=PENDING, server_default=PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, server_default="5")

    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    # While a job is running, other workers leave it alone until this time. If
    # the worker dies mid-job, the lock simply expires and the job is retried.
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)

    # Optional. Enqueuing a job whose dedupe_key already exists does nothing,
    # so "send the confirmation for order X" can never be queued twice.
    dedupe_key: Mapped[str | None] = mapped_column(String(255), unique=True, default=None)
