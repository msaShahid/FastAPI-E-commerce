# Response caching (Redis)

The two busiest public endpoints are cached in Redis:

| Endpoint | Cached | TTL (safety net) |
|---|---|---|
| `GET /api/v1/categories` | per page | `CACHE_TTL_CATEGORIES_SECONDS` (300) |
| `GET /api/v1/products` | per combination of page, page size, category, price range and sort | `CACHE_TTL_PRODUCTS_SECONDS` (60) |

Everything else is **not** cached on purpose: single products and categories
(a primary-key lookup is already cheap), carts, orders, payments, anything
that depends on who is asking, and GraphQL.

Every cached response carries an `X-Cache` header:

| Value | Meaning |
|---|---|
| `HIT` | Served from Redis. No SQL ran. |
| `MISS` | Not in the cache; the database answered and the result was stored. |
| `BYPASS` | The cache was not used for this request: it is an admin request, a `search=` request, or Redis is unavailable. |
| *(absent)* | `CACHE_ENABLED=false`. |

## What is deliberately not cached

* **Admin requests.** An admin's product list also contains draft and
  archived products. Admins always read the database and never fill the
  public entries.
* **`search=` requests.** Nearly every search string is different, so these
  would fill Redis with entries that are never read twice.
* **Nothing is cached per user.** The public list is identical for every
  caller, so logged-in and anonymous shoppers share the same entries.

## How entries are kept fresh

TTLs alone would mean showing a wrong price or stock for up to a minute after
an edit. The TTL is only a safety net. Freshness comes from invalidation:

1. Each namespace (`categories`, `products`) has a **version number** in Redis.
2. An entry is stored as `"<version>|<json>"` and is served **only while its
   version equals the namespace's current version**.
3. Invalidating a namespace is one `INCR`. Every entry written under the old
   version is dead immediately; nothing is scanned or deleted (Redis expires
   the dead entries by TTL).

**When does invalidation happen?** A SQLAlchemy hook (`after_flush`) notices
whenever a `products` or `categories` row is inserted, updated or deleted and
remembers the affected namespaces on the session. `get_db` then invalidates
**right after the commit**. Because it hangs off the database session, it
covers every write path without any of them having to remember:

* admin create/update/archive/image endpoints (REST)
* GraphQL mutations
* checkout (stock goes down) and the stale-order job (stock goes back up)
* anything added in future that writes those tables

| Change | Invalidates |
|---|---|
| a product row | `products` |
| a category row | `categories` **and** `products` (a product list embeds its category) |

A request that fails and rolls back invalidates nothing.

> Stock changes on every checkout, so under heavy checkout traffic the
> `products` entries are replaced often. That is the price of never showing
> stale stock. If checkout volume ever makes the hit rate too low, the
> alternative is to exclude stock from the cached list and use a short TTL.

### Why the order of operations matters

* **Invalidate after commit, never before.** Before the commit, a concurrent
  reader would re-fill the cache from the old data, and that entry would then
  be considered current.
* **A reader reads the version before it queries the database** and tags what
  it stores with that version. If a write commits while the reader is
  working, the reader's possibly-stale result is stored under the old version
  and is never served. (`test_a_read_that_overlaps_a_write_cannot_leave_stale_data_in_the_cache`.)

## Stampede protection

When an entry is missing and many requests ask for it at once, only one of
them queries the database; the others wait for that result. (Per API process;
across several processes at most one query per process runs.) Waiters only
share a result read under the same version, so a request that arrives after a
write never gets an answer that was read before it.

## When Redis is unavailable

The cache is **fail-open**:

* Redis commands time out after `CACHE_REDIS_TIMEOUT_SECONDS` (0.25 s).
* On any Redis error the request is answered from the database
  (`X-Cache: BYPASS`).
* For `CACHE_FAILURE_COOLDOWN_SECONDS` (5 s) after a failure Redis is not
  contacted at all, so a dead Redis does not add a timeout to every request.
  Measured: 50 requests during the cooldown took under 0.1 s in total.
* An invalidation that could not reach Redis is remembered and replayed as
  soon as Redis answers again, before the next cache read.
* If the Redis server loses its data (restart without persistence, a flush,
  eviction of a version key), nothing stale can come back: a missing version
  key restarts at the current time in microseconds, larger than any version
  it could have had, so old entries never match.

**Remaining limit:** if an invalidation is lost *and* the process that lost it
also stops before Redis recovers, entries can stay stale until their TTL. That
is what the TTL is for, and why products have a short one.

> Separate from the cache: the **login/register rate limiter** (slowapi) uses
> the same Redis and currently returns HTTP 500 on the rate-limited endpoints
> when Redis is unreachable (checked by stopping Redis under a running API).
> The cache is unaffected. slowapi can fall back to per-process counters
> instead (`Limiter(..., in_memory_fallback_enabled=True)` in
> `app/core/rate_limit.py`); that is a decision about how strict rate limiting
> must be during an outage, so it is not changed here.

## Redis configuration

* Use the same `REDIS_URL` as the rate limiter. Cache keys are prefixed with
  `CACHE_KEY_PREFIX` (default `shop`) so they cannot collide with it or with
  another environment sharing the Redis.
* Version keys have **no TTL**. If you set a `maxmemory`, use a `volatile-*`
  policy (for example `volatile-lru`) or `noeviction`, not `allkeys-*`, so
  version keys are never evicted. (Even if one is, the behaviour above keeps
  the data correct; it just costs a cache refill.)
* Memory use: one entry per distinct page/filter/sort combination, a few KB
  each, expiring after the TTL.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `CACHE_ENABLED` | `true` | Master switch. `false` = every request goes to the database, no `X-Cache` header. |
| `CACHE_KEY_PREFIX` | `shop` | Prefix for all cache keys. |
| `CACHE_TTL_CATEGORIES_SECONDS` | `300` | Safety-net expiry for category lists. |
| `CACHE_TTL_PRODUCTS_SECONDS` | `60` | Safety-net expiry for product lists. |
| `CACHE_REDIS_TIMEOUT_SECONDS` | `0.25` | Per-command Redis timeout. |
| `CACHE_FAILURE_COOLDOWN_SECONDS` | `5` | How long Redis is skipped after a failure. |

## Measured effect

Single uvicorn process, 40 concurrent virtual users, read-only mix of product
lists (pages, sorts, category filters) and category lists, against 200,000
products (the data set from `docs/index-audit.md`), load generator on the same
machine:

| | Requests/s | Median | p95 | p99 | Failures |
|---|---|---|---|---|---|
| `CACHE_ENABLED=false` | 91 | 400 ms | 620 ms | 770 ms | 0 |
| `CACHE_ENABLED=true` | 463 | 54 ms | 100 ms | 170 ms | 0 |

A hit runs no SQL and uses no database connection, so cached traffic also
stops competing for the connection pool (see `docs/connection-pool.md`).
Absolute numbers depend on hardware; the ratio is the useful part.

## Testing

`tests/integration/test_cache_integration.py` uses a real Redis and Postgres.
The test suite switches the cache **off** for every other test (an autouse
fixture in `tests/conftest.py`), so one test's cached response can never leak
into another. The `get_db` override in `tests/conftest.py` also calls
`flush_pending`, matching the real `get_db`; one test drives the real `get_db`
to prove it does.

## Adding another cached endpoint

1. Pick a namespace and add the tables whose rows it contains to
   `_TABLE_NAMESPACES` in `app/core/cache.py`.
2. In the router, wrap the database work in a `load()` function that returns a
   JSON string, and call `get_cache().get_or_set(namespace, params, ttl, load)`.
   `params` must contain **everything the response depends on**.
3. If the response depends on who is asking, do not cache it (pass
   `cacheable=False`) or put the distinguishing part in `params`.
4. Add tests for hit, miss and invalidation by every write path.
