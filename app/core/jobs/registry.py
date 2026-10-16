from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# A handler receives the job's payload and the worker's session factory (so it
# opens its own short database sessions and a test can point it at the test
# database). It must be IDEMPOTENT: a job can run more than once (a worker that
# dies after the work but before recording success, a retry after a timeout).
JobHandler = Callable[[dict, async_sessionmaker[AsyncSession]], Awaitable[None]]

_handlers: dict[str, JobHandler] = {}


def job_handler(job_type: str) -> Callable[[JobHandler], JobHandler]:
    """Register the function that runs jobs of this type."""

    def register(func: JobHandler) -> JobHandler:
        if job_type in _handlers and _handlers[job_type] is not func:
            raise RuntimeError(f"A handler for job type {job_type!r} is already registered")
        _handlers[job_type] = func
        return func

    return register


def get_handler(job_type: str) -> JobHandler | None:
    return _handlers.get(job_type)
