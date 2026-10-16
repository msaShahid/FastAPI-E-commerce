"""
Runs the background job worker as its own process.

    docker compose exec api python -m scripts.run_worker      # try it
    # in production: a separate container/service running the same command

Stops cleanly on SIGTERM / Ctrl-C: the job it is running finishes first. You can
run several of these; they never pick up the same job (see docs/background-jobs.md).
"""

import asyncio
import logging
import signal

import app.jobs  # noqa: F401  (registers the job handlers)
from app.core.config import get_settings
from app.core.database import engine
from app.core.jobs.worker import run_worker


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    config = get_settings()
    logging.getLogger(__name__).info(
        "Job worker started (concurrency=%d, poll=%ss)",
        config.jobs_concurrency,
        config.jobs_poll_interval_seconds,
    )
    await run_worker(stop)
    await engine.dispose()
    logging.getLogger(__name__).info("Job worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
