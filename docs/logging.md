# Logging and request ids

Done when: (1) one request's log lines can be pulled out of a shared log stream
by its request id alone, and (2) an error response carries an id the customer
can quote to support.

## What you get

Every log line is one JSON object:

```json
{"timestamp": "2026-10-16T10:18:55.411+00:00", "level": "ERROR", "logger": "app.core.exception_handlers",
 "message": "Unhandled exception on GET /api/v1/products/12345",
 "request_id": "demo-from-customer-1", "user_id": "3f1c...", "method": "GET",
 "route": "/api/v1/products/{product_id}", "path": "/api/v1/products/12345",
 "exception": "Traceback (most recent call last): ..."}
```

`request_id`, `method`, `route` and `path` are on every line written while a
request is handled, `user_id` once the caller is authenticated, and `job_id` /
`job_type` on every line written while a background job runs. Fields passed
with `extra={...}` become top-level keys.

Existing code did not change: it still calls `logging.getLogger(__name__)`. A
handler filter stamps the context onto each record (`app/core/logging_config.py`).

## Request ids

`app/core/request_context.py` (outermost middleware):

* uses the caller's `X-Request-ID` if it is 8-64 characters of `A-Z a-z 0-9 . _ -`,
  otherwise generates a random 32-hex id. (Anything else could forge log lines or
  bloat them; a bad value is replaced, not rejected.)
* returns it in the `X-Request-ID` response header on every response, including
  CORS preflights. CORS exposes the header, so browser JavaScript can show it.
* includes it as `request_id` in every error body (the app's own errors, plus
  FastAPI's 404/405/422, whose `detail` shape is unchanged). The 429 from the rate
  limiter has the header only.
* writes one access line per request (`logger: app.access`) with `status` and
  `duration_ms`. It names the route template (`/api/v1/products/{product_id}`), so
  lines group by endpoint instead of by id. The query string and headers are never
  logged. 5xx is `ERROR`; `/health`, `/health/db`, `/metrics` are `DEBUG` (polled
  constantly) unless they fail.

### Finding a customer's request

Support asks for the id in the error (`{"detail": "...", "request_id": "abc..."}`),
then:

```bash
docker compose logs api | grep abc123...            # a shared stream
docker compose logs api | jq 'select(.request_id == "abc123...")'
```

## Settings

| Variable | Default | |
|---|---|---|
| `LOG_FORMAT` | `json` | `text` gives readable lines for local development |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |

uvicorn's own access log is switched off (ours replaces it) and its server
messages go through the same formatter.

## How it works, and what to know

* The context is a dict in a `ContextVar`, shared by everything in the request,
  so a value added later (user id after auth, route after routing) shows up on
  later lines, including from sync endpoints run in a thread pool. It is reset
  when the request ends, so nothing leaks into the next one.
* Starlette runs the catch-all 500 handler outside the middleware, so that handler
  re-activates the request's context itself before logging the traceback, and the
  error body and header get the id explicitly. After it logs, it marks the
  exception so uvicorn's second, id-less copy ("Exception in ASGI application")
  is dropped instead of duplicating every traceback.
* Not covered: log lines from a task started during a request but outliving it
  (it keeps the context it was created with), and the job-to-request link (a job
  has its own `job_id`; it does not record which request queued it).
* This is plain `logging`, not structlog: no new dependency, and library logs
  (SQLAlchemy, uvicorn, strawberry) come out in the same format for free.

## Rules for new code

* Log with `logging.getLogger(__name__)`; put variable data in `extra={...}` when
  you want to filter on it, e.g. `logger.info("order paid", extra={"order_id": id})`.
* Never log tokens, passwords, card data or whole request bodies.
* Jobs: nothing to do; the worker binds `job_id` / `job_type` around each handler.
