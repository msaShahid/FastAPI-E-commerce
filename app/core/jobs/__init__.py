"""
Background jobs: work that should not make a request wait.

    from app.core.jobs import JobQueue

    await JobQueue(db).enqueue("send_order_confirmation", {"order_id": "..."})

The job is saved in the caller's own database transaction, so it exists if and
only if the change that needed it was committed. A separate worker process
(`python -m scripts.run_worker`) runs it later, with retries. See
docs/background-jobs.md.
"""

from app.core.jobs.queue import JobQueue
from app.core.jobs.registry import job_handler

__all__ = ["JobQueue", "job_handler"]
