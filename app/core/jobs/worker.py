"""
The job worker: claim a due job, run its handler, record the outcome.

Safe to run as many workers as you like (and several per process): a job is
claimed with `FOR UPDATE SKIP LOCKED`, so two workers never get the same one.

Delivery is AT LEAST ONCE. A worker that dies after doing the work but before
recording success leaves the job `running`; when its lock expires the job runs
again. Handlers must therefore be idempotent.
"""

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.database import async_session_factory
from app.core.jobs.registry import get_handler
from app.core.logging_config import bound_context, new_context

logger = logging.getLogger(__name__)

_FAIL_EXHAUSTED_SQL = text(
    """
    UPDATE background_jobs
    SET status = 'failed', finished_at = now(), locked_until = NULL,
        last_error = coalesce(last_error, '') || ' [worker stopped before finishing; out of attempts]',
        updated_at = now()
    WHERE status = 'running' AND locked_until < now() AND attempts >= max_attempts
    """
)

# One short transaction. SKIP LOCKED makes concurrent workers pass over a row
# another worker is claiming instead of waiting for it. Besides pending jobs
# that are due, it also takes `running` jobs whose lock has expired (their
# worker died).
_CLAIM_SQL = text(
    """
    UPDATE background_jobs
    SET status = 'running',
        attempts = attempts + 1,
        locked_until = now() + make_interval(secs => CAST(:lock_seconds AS double precision)),
        updated_at = now()
    WHERE id = (
        SELECT id FROM background_jobs
        WHERE attempts < max_attempts
          AND ((status = 'pending' AND run_at <= now())
               OR (status = 'running' AND locked_until < now()))
        ORDER BY run_at, id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, job_type, payload, attempts, max_attempts
    """
)

# The `attempts = :attempts` guard: if this worker was so slow that its lock
# expired and another worker claimed the job (bumping attempts), this worker's
# late result must not overwrite the newer attempt's state.
_COMPLETE_SQL = text(
    """
    UPDATE background_jobs
    SET status = 'succeeded', finished_at = now(), locked_until = NULL,
        last_error = NULL, updated_at = now()
    WHERE id = :id AND status = 'running' AND attempts = :attempts
    """
)

_FAIL_SQL = text(
    """
    UPDATE background_jobs
    SET status = CASE WHEN :final THEN 'failed' ELSE 'pending' END,
        run_at = now() + make_interval(secs => CAST(:delay_seconds AS double precision)),
        finished_at = CASE WHEN :final THEN now() END,
        locked_until = NULL,
        last_error = :error,
        updated_at = now()
    WHERE id = :id AND status = 'running' AND attempts = :attempts
    """
)


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    job_type: str
    payload: dict
    attempts: int
    max_attempts: int


def retry_delay_seconds(attempts: int, *, base: float, cap: float) -> float:
    """Delay before the next try, after `attempts` tries so far: base, 2*base, 4*base ... up to cap."""
    return min(base * 2 ** max(attempts - 1, 0), cap)


async def claim_next(
    session_factory: async_sessionmaker[AsyncSession], config: Settings
) -> ClaimedJob | None:
    async with session_factory() as session, session.begin():
        await session.execute(_FAIL_EXHAUSTED_SQL)
        row = (
            await session.execute(_CLAIM_SQL, {"lock_seconds": config.jobs_lock_seconds})
        ).first()
    if row is None:
        return None
    return ClaimedJob(
        id=row.id,
        job_type=row.job_type,
        payload=row.payload,
        attempts=row.attempts,
        max_attempts=row.max_attempts,
    )


async def complete_job(
    session_factory: async_sessionmaker[AsyncSession], job: ClaimedJob
) -> bool:
    async with session_factory() as session, session.begin():
        result = await session.execute(_COMPLETE_SQL, {"id": job.id, "attempts": job.attempts})
    return result.rowcount == 1


async def fail_job(
    session_factory: async_sessionmaker[AsyncSession],
    job: ClaimedJob,
    error: str,
    config: Settings,
) -> bool:
    final = job.attempts >= job.max_attempts
    delay = retry_delay_seconds(
        job.attempts,
        base=config.jobs_retry_base_seconds,
        cap=config.jobs_retry_max_seconds,
    )
    async with session_factory() as session, session.begin():
        result = await session.execute(
            _FAIL_SQL,
            {
                "id": job.id,
                "attempts": job.attempts,
                "final": final,
                "delay_seconds": delay,
                "error": error[:4000],
            },
        )
    return result.rowcount == 1


async def run_one(
    session_factory: async_sessionmaker[AsyncSession] = async_session_factory,
    config: Settings | None = None,
) -> bool:
    """Claim and run one due job. Returns False if nothing was due."""
    config = config or get_settings()
    job = await claim_next(session_factory, config)
    if job is None:
        return False

    # Every log line while this job runs (including the handler's) carries its id.
    with bound_context(new_context(job_id=str(job.id), job_type=job.job_type)):
        await _run_claimed(job, session_factory, config)
    return True


async def _run_claimed(
    job: ClaimedJob,
    session_factory: async_sessionmaker[AsyncSession],
    config: Settings,
) -> None:
    started = time.monotonic()
    handler = get_handler(job.job_type)
    try:
        if handler is None:
            raise LookupError(f"no handler registered for job type {job.job_type!r}")
        await asyncio.wait_for(
            handler(job.payload, session_factory), timeout=config.jobs_job_timeout_seconds
        )
    except Exception as exc:  # includes TimeoutError from wait_for
        error = f"{type(exc).__name__}: {exc}"
        final = job.attempts >= job.max_attempts
        log = logger.error if final else logger.warning
        log(
            "job %s (%s) failed on attempt %d/%d%s: %s",
            job.id,
            job.job_type,
            job.attempts,
            job.max_attempts,
            " -- giving up" if final else "",
            error,
            exc_info=final,
        )
        await fail_job(session_factory, job, error, config)
    else:
        if not await complete_job(session_factory, job):
            logger.warning(
                "job %s (%s) finished, but its lock had expired and another worker took "
                "it over; its result is ignored",
                job.id,
                job.job_type,
            )
        logger.info(
            "job %s (%s) succeeded on attempt %d in %.2fs",
            job.id,
            job.job_type,
            job.attempts,
            time.monotonic() - started,
        )


async def run_worker(
    stop: asyncio.Event,
    *,
    session_factory: async_sessionmaker[AsyncSession] = async_session_factory,
    config: Settings | None = None,
    concurrency: int | None = None,
) -> None:
    """
    Run jobs until `stop` is set. A job that is already running when `stop` is
    set is allowed to finish.
    """
    config = config or get_settings()

    async def loop() -> None:
        while not stop.is_set():
            try:
                did_work = await run_one(session_factory, config)
            except Exception:  # e.g. the database is briefly unreachable
                logger.exception("job worker could not reach the queue; will retry")
                did_work = False
            if did_work:
                continue  # there may be more waiting; do not sleep
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=config.jobs_poll_interval_seconds)

    from app.core.jobs.schedule import run_scheduler

    await asyncio.gather(
        *(loop() for _ in range(concurrency or config.jobs_concurrency)),
        run_scheduler(stop, session_factory, config),
    )
