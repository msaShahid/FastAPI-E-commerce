"""
Periodic jobs. A module registers "enqueue this job type every N seconds"; every worker process runs the
scheduler loop, and the dedupe key (job type + time bucket) makes several workers enqueue it ONCE per
interval. Nothing is scheduled unless the module registers it and its interval setting is above 0.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.jobs.queue import JobQueue

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Schedule:
    job_type: str
    interval_seconds: Callable[[Settings], int]


_SCHEDULES: list[Schedule] = []


def register_schedule(job_type: str, interval_seconds: Callable[[Settings], int]) -> None:
    if not any(s.job_type == job_type for s in _SCHEDULES):
        _SCHEDULES.append(Schedule(job_type, interval_seconds))


def get_schedules() -> list[Schedule]:
    return list(_SCHEDULES)


async def enqueue_due(session_factory: async_sessionmaker[AsyncSession], config: Settings) -> list[str]:
    """Enqueues each scheduled job for the current time bucket. Safe to call from many workers at once."""
    enqueued: list[str] = []
    for schedule in get_schedules():
        interval = schedule.interval_seconds(config)
        if interval <= 0:
            continue  # disabled
        bucket = int(time.time() // interval)
        async with session_factory() as session, session.begin():
            job_id = await JobQueue(session).enqueue(
                schedule.job_type, {}, dedupe_key=f"schedule:{schedule.job_type}:{bucket}", max_attempts=3
            )
        if job_id is not None:
            enqueued.append(schedule.job_type)
    return enqueued


async def run_scheduler(
    stop: asyncio.Event, session_factory: async_sessionmaker[AsyncSession], config: Settings
) -> None:
    intervals = [s.interval_seconds(config) for s in get_schedules() if s.interval_seconds(config) > 0]
    if not intervals:
        return
    tick = max(1.0, min(intervals) / 2)  # twice per shortest interval, so a bucket is never skipped
    while not stop.is_set():
        try:
            await enqueue_due(session_factory, config)
        except Exception:  # e.g. the database is briefly unreachable: try again next tick
            logger.exception("job scheduler could not enqueue; will retry")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=tick)
