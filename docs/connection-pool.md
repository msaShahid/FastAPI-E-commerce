# Database connection pool

Stage 4, item 2. Opening a database connection is slow (a network handshake,
authentication, a server process), so the app keeps a **pool** of open
connections and lends one to each request. Two things go wrong with a pool that
is not sized deliberately: too small and requests queue or fail; too large and
the database runs out of connections or wastes memory and CPU on contention.

## Settings

Set these as environment variables (see `.env.example`). Each applies **per API
process**.

| Variable | Default | Meaning |
|---|---|---|
| `DB_POOL_SIZE` | 5 | Connections kept open. |
| `DB_MAX_OVERFLOW` | 10 | Extra connections opened under load and closed again when idle. |
| `DB_POOL_TIMEOUT` | 10 | Seconds a request waits for a free connection before the API answers **503**. |
| `DB_POOL_RECYCLE` | 1800 | Replace connections older than this many seconds (`-1` = never). |
| `DB_POOL_PRE_PING` | true | Test a connection before using it, so one the server closed while idle is replaced and not used. |
| `DB_DISABLE_PREPARED_STATEMENTS` | false | Only for transaction-mode poolers without prepared-statement support (see below). |

Pool size and overflow are unchanged from before (5 + 10). The timeout is now
10 s instead of SQLAlchemy's 30 s, and pre-ping and recycling are new.

## How to size it

```
connections the database may see  =  processes  x  (DB_POOL_SIZE + DB_MAX_OVERFLOW)
```

That number, summed over **every** API process and replica, plus anything else
that connects (migrations, `psql`, admin tools, other services), must stay below
Postgres's `max_connections` (default 100, of which 3 are reserved for
superusers).

Examples with the defaults (15 per process):

| Deployment | Worst case | Fits in `max_connections=100`? |
|---|---|---|
| 1 process (what the Dockerfile starts today) | 15 | yes |
| 4 workers | 60 | yes |
| 4 workers x 2 replicas | 120 | **no**: lower the pool or raise `max_connections` |

More is not better. A pool larger than the database can actually work on just
queues the work inside Postgres instead of in the app (see the measurements).
Start with the defaults, watch for pool-exhaustion log lines, and only raise the
numbers if you see them *and* the database has headroom.

## What it looks like when the pool runs dry

A request that cannot get a connection within `DB_POOL_TIMEOUT` seconds now
gets **HTTP 503** with `Retry-After: 1` and the message "The service is busy.
Please retry in a moment.", instead of an unexplained 500. The server logs
`Database connection pool exhausted on <METHOD> <path>` at ERROR level; alert on
that line (Stage 5).

## Measurements

Sandbox with 2 CPU cores shared by the API (2 workers), Postgres and the load
generator, using the Stage 3 Locust test at 200 users. The absolute numbers say
little; the behaviour is the point.

| Configuration | Connections seen by Postgres | Pool errors | p95 latency |
|---|---|---|---|
| Default, 5 + 10 per worker | **30** (= 2 x 15) | 0 | about 1.0 s (3 runs: 0.97, 1.1, 1.0) |
| Large, 20 + 20 per worker | 80 (= 2 x 40) | 0 | about 1.2-1.6 s (3 runs: 1.4, 1.6, 1.2) |
| Tiny, 1 + 0 per worker, 1 s timeout | 2 | 12 requests got a clean 503 | n/a |

* The observed connection count matches the formula exactly.
* The default pool was fully used at 200 users but never exhausted: requests are
  short, so they take turns on the 15 connections.
* The larger pool did **not** help; it was slightly slower, because the CPU was
  the limit and more concurrent queries only add contention.

## The real cause of exhausted pools: slow work while holding a connection

A connection is held from a request's first query until it commits at the end.
If the request does something slow in the middle, it pins a connection the whole
time. Currently `PaymentService.create_payment_for_order` locks the order row and
then calls Stripe (over the network) before it commits, so each in-flight
payment request holds one connection, and the row lock, until Stripe replies.
This is deliberate (the lock stops a double-click creating two payment intents),
but a Stripe slowdown means 15 slow payment requests can use up every connection
a process has, and every unrelated endpoint starts returning 503. I read this
from the code; I did not load-test it. The fix is to restructure the flow so the
slow call happens without a connection held (reserve, call Stripe, then record),
which belongs with item 4.4 (work that should not block a request).

## Connection poolers (PgBouncer, Neon's `-pooler` endpoint)

A pooler sits between the app and Postgres and shares a few real database
connections among many client connections. The app's own pool still applies, but
the database sees only the pooler's connections. In a test with PgBouncer 1.22
(the version Neon documents) in transaction mode, 100 users produced 10
database connections, the pooler's `default_pool_size`.

The catch is **prepared statements**. asyncpg (the driver) uses them, and in
transaction mode consecutive statements from one client may run on different
server connections:

| PgBouncer setting | App setting | Result (100 users, Stage 3 load test) |
|---|---|---|
| `max_prepared_statements=200` | default | works |
| `max_prepared_statements=0` (off) | default | **fails**: `prepared statement "__asyncpg_stmt_a__" does not exist`, thousands of errors |
| `max_prepared_statements=0` (off) | `DB_DISABLE_PREPARED_STATEMENTS=true` | works |

PgBouncer 1.21 added support for prepared statements in transaction mode, but
it must be switched on with `max_prepared_statements`. Neon states that it runs
PgBouncer 1.22; I could not test against Neon itself, so do not assume: connect
through your pooled URL on a **non-production** database, run a few requests,
and only if you see "prepared statement ... does not exist" set
`DB_DISABLE_PREPARED_STATEMENTS=true`.

The same setting applies to Alembic. Behind a pooler with prepared statements
off, `alembic upgrade head` worked but `alembic check` failed (6 of 6 runs
without the setting, 0 of 6 with it). If you ever have trouble running
migrations through a pooler, run them against the database's direct
(non-pooled) address instead.
