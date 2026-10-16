# Background jobs

Some work should not make a customer (or Stripe) wait: sending an email,
generating an invoice, calling a slow third party. This project has a small
job queue for that. Today it runs one job -- the **order confirmation email**,
queued when the Stripe webhook marks an order paid -- and it is the place to
put the next slow thing.

```
webhook request                          worker process (separate)
-------------------------------          ---------------------------------
verify signature                         loop:
claim the event                            claim a due job (FOR UPDATE SKIP LOCKED)
mark payment + order PAID                  run its handler (e.g. send the email)
INSERT job "send_order_confirmation"       mark it succeeded / schedule a retry
COMMIT  -------- one transaction -------
answer 200 (does not wait for the email)
```

## Why a table in Postgres, not Celery / arq / `BackgroundTasks`

* **The job is saved in the same transaction as the change that needs it.**
  "The order is PAID" and "the confirmation is queued" either both happen or
  neither does. With Redis/Celery you enqueue *after* the commit; a crash in
  between loses the job, or you enqueue first and the job runs for a change
  that was rolled back.
* **FastAPI `BackgroundTasks` are lost on a crash or restart** and cannot
  retry. They run in the API process, so a slow job still competes with
  requests.
* **No new dependency or service.** Only the worker process is new.
* It is easy to look at: `SELECT * FROM background_jobs WHERE status = 'failed'`.

The trade-off: polling Postgres (one cheap indexed query per second per
worker slot while idle) instead of a push from a broker, so a job starts up to
`JOBS_POLL_INTERVAL_SECONDS` (1 s) after it is queued, and it is not meant for
thousands of jobs per second. If you outgrow it, the call sites
(`JobQueue.enqueue`, `@job_handler`) are the only things that would change.

## Guarantees

* **At least once.** A job can run more than once -- for example the worker
  dies after sending the email but before recording success. **Handlers must
  be idempotent** (or tolerate a rare duplicate).
* **Never two workers on one job at the same time.** A job is claimed with
  `FOR UPDATE SKIP LOCKED`. Run as many workers as you like.
* **Retries with exponential backoff:** after attempt *n* the next try is in
  `JOBS_RETRY_BASE_SECONDS * 2^(n-1)`, at most `JOBS_RETRY_MAX_SECONDS`
  (30 s, 60 s, 120 s ... up to 1 h). After `max_attempts` (default 5) the job
  is `failed` and stays in the table with its `last_error`.
* **Worker crash recovery.** A claimed job is locked for `JOBS_LOCK_SECONDS`
  (300 s). If the worker dies, the lock expires and another worker takes it
  over. A late result from a worker that already lost its lock is ignored.
* **A time limit per attempt** (`JOBS_JOB_TIMEOUT_SECONDS`, 60 s) so one stuck
  call cannot hold a worker slot forever. The lock must outlast it; startup
  fails if `JOBS_LOCK_SECONDS <= JOBS_JOB_TIMEOUT_SECONDS`.
* **Deduplication.** `enqueue(..., dedupe_key="order-confirmation:<id>")`
  does nothing if a job with that key already exists, so the same email cannot
  be queued twice even if the webhook code ran again.
* All times (`run_at`, locks) are evaluated by Postgres with `now()`, so
  workers whose clocks differ still agree.

## Running the worker

**Production -- its own process** (so a slow job never competes with requests,
and you can scale it separately). Add a service next to `api` in
`docker-compose.yml` / `docker-compose.prod.yml`:

```yaml
  worker:
    build:
      context: .
    command: python -m scripts.run_worker
    env_file:
      - .env
    environment:
      PYTHONPATH: /code
    depends_on:
      db:
        condition: service_healthy
      redis:
        condition: service_healthy
    restart: unless-stopped
```

(Use the same `build`/`env_file`/`depends_on` as your `api` service.) It stops
cleanly on SIGTERM: the job it is running finishes first, so give the container
a stop grace period a little longer than `JOBS_JOB_TIMEOUT_SECONDS`.

**Development / small deployments -- inside the API.** Set
`JOBS_EMBEDDED_WORKER=true` and the API process runs the worker itself; no
extra service. Several API processes each run one; that is safe. The
trade-off is that jobs share the process with request handling.

**Trying it by hand:**

```bash
docker compose exec api python -m scripts.run_worker
```

## Email

`app/core/email/` has an `EmailSender` interface with one implementation,
`LoggingEmailSender`, which **writes the email to the log instead of sending
it**, so everything works without a mail provider. To send real mail, write an
`EmailSender` for your provider (SMTP, SES, SendGrid ...) and return it from
`get_email_sender()`. In `production` the app logs a warning until you do.
A failing `send()` should raise: the job is then retried.

## Adding a job

1. Write the handler (anywhere in a module; here `app/modules/payments/jobs.py`):

   ```python
   @job_handler("generate_invoice")
   async def generate_invoice(payload: dict, session_factory) -> None:
       async with session_factory() as session:
           ...  # open your own short session; make it safe to run twice
   ```

2. Make sure the worker imports it: add the module to `app/jobs.py`.
3. Queue it from the request, in the request's own transaction:

   ```python
   await JobQueue(db).enqueue("generate_invoice", {"order_id": str(order.id)},
                              dedupe_key=f"invoice:{order.id}")
   ```

Keep payloads small and JSON-only (ids, not whole objects); the handler loads
fresh data when it runs.

## Operating it

```sql
-- what is waiting / running / done / given up on
SELECT status, count(*) FROM background_jobs GROUP BY status;

-- jobs that ran out of attempts, newest first
SELECT id, job_type, attempts, last_error, payload
FROM background_jobs WHERE status = 'failed' ORDER BY id DESC;

-- retry a failed job once you have fixed the cause
UPDATE background_jobs
SET status = 'pending', attempts = 0, run_at = now(), last_error = NULL, finished_at = NULL
WHERE id = 123;

-- tidy up (finished jobs are never deleted automatically)
DELETE FROM background_jobs WHERE status = 'succeeded' AND finished_at < now() - interval '30 days';
```

Alert on `failed` rows, and on `pending` jobs whose `run_at` is far in the
past (no worker is running). Worker logs show every job's id, type, attempt
and duration.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `JOBS_EMBEDDED_WORKER` | `false` | Run the worker inside the API process. |
| `JOBS_CONCURRENCY` | `2` | Jobs one worker runs at the same time. |
| `JOBS_POLL_INTERVAL_SECONDS` | `1` | How long an idle worker sleeps between checks. |
| `JOBS_DEFAULT_MAX_ATTEMPTS` | `5` | Tries before a job is marked `failed`. |
| `JOBS_JOB_TIMEOUT_SECONDS` | `60` | Time limit for one attempt. |
| `JOBS_LOCK_SECONDS` | `300` | How long a claimed job is invisible to others (must exceed the timeout). |
| `JOBS_RETRY_BASE_SECONDS` | `30` | First retry delay; doubles each time. |
| `JOBS_RETRY_MAX_SECONDS` | `3600` | Upper limit for the retry delay. |

## What was checked

* `tests/integration/test_background_jobs.py` (23 tests, real Postgres):
  atomic enqueue and rollback, dedupe, delay, retry/backoff, giving up after
  `max_attempts`, per-attempt timeout, crash takeover, late results ignored,
  60 jobs on 8 concurrent workers each run exactly once, graceful stop, the
  embedded worker, and the webhook.
* **The acceptance test** (`test_slow_email_provider_does_not_slow_the_webhook`):
  the email provider is made to take 1 s; the webhook answers in well under
  0.5 s with the order already PAID and the job already queued, nothing was
  sent during the request, and the worker then sends exactly one email.
* Two real `python -m scripts.run_worker` processes on 60 queued jobs: they
  split the work 25 / 35, no recipient was emailed twice, and both stopped
  cleanly on SIGTERM.

## Not covered here

The Stripe calls for *creating a payment* and *refunding* are different: the
customer needs the answer (the client secret), so they cannot be deferred.
They currently run while the request holds a database connection and the order
row lock (found while sizing the pool, see `docs/connection-pool.md`).
Shortening that is a separate change to how those requests use transactions.
