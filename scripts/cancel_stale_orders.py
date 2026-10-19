"""
Queues ONE order-expiry sweep right now (the worker normally does this on a schedule; see
docs/STATUS.md). It does not cancel anything itself: the worker's expiry job cancels each stale
order safely (payment intent first, then cancel + restock together).

    docker compose exec api python -m scripts.cancel_stale_orders

Requires a running worker:  docker compose up -d worker
"""

import asyncio

import app.jobs  # noqa: F401  (registers the handlers)
from app.core.database import async_session_factory, engine
from app.core.jobs.queue import JobQueue
from app.modules.payments.job_types import SWEEP_STALE_ORDERS


async def main() -> None:
    async with async_session_factory() as session, session.begin():
        job_id = await JobQueue(session).enqueue(SWEEP_STALE_ORDERS, {}, max_attempts=3)
    print(f"queued sweep job {job_id}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
