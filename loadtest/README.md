# Load test

A [Locust](https://locust.io) script that drives the API like real shoppers
and then **checks that checkout never oversold anything**.

## What it does

| Virtual user | Share | Behaviour |
|---|---|---|
| `BrowserUser` | 70% | Product list, search + sort, product detail, categories (read-only). |
| `ShopperUser` | 30% | Guest shoppers all racing for **one limited-stock product**: add 1 unit to the cart, check out, and sometimes replay the same checkout request (same `Idempotency-Key`). |

When the run ends it reads the product back and verifies:

- `stock left == initial stock - orders created` (no lost or phantom stock)
- `stock left >= 0` (never oversold)
- no request failed unexpectedly (any 5xx or unexpected status)
- overall p95 latency is under `LOADTEST_MAX_P95_MS` (default 2000 ms)

It prints a `CHECKOUT INVARIANT` summary with `RESULT: PASS` or `FAIL` and
exits non-zero on failure.

"Sold out" (HTTP 422 on add-to-cart or checkout) is an **expected** outcome
here, not a failure. Guest checkout is used on purpose: `/auth/register` and
`/auth/login` are rate limited per IP, and a load test comes from one IP, so
creating a user per virtual user would only measure the rate limiter.

## Run it

> **This writes data** (a category, a product, guest carts and orders).
> Run it against your local/dev stack only. The script refuses hosts other
> than `localhost`, `127.0.0.1`, `api` and `host.docker.internal` unless you
> set `LOADTEST_ALLOW_REMOTE=1`; never point it at production.

1. Make sure you have an admin account (needed to create the limited-stock
   product):

   ```bash
   docker compose exec api python -m scripts.bootstrap_admin --email loadtest-admin@example.com
   ```

2. Add its credentials to your (git-ignored) `.env`:

   ```
   LOADTEST_ADMIN_EMAIL=loadtest-admin@example.com
   LOADTEST_ADMIN_PASSWORD=the-password-you-chose
   ```

3. Start the stack and run the test (headless, 50 users, 30 seconds):

   ```bash
   docker compose up -d
   docker compose -f docker-compose.yml -f loadtest/docker-compose.loadtest.yml run --rm --build loadtest
   ```

### Options

Override the defaults by appending Locust arguments:

```bash
# heavier: 200 users, 60 seconds
docker compose -f docker-compose.yml -f loadtest/docker-compose.loadtest.yml run --rm loadtest \
    -f locustfile.py --headless --host http://api:8000 -u 200 -r 50 -t 60s

# interactive web UI at http://localhost:8089
docker compose -f docker-compose.yml -f loadtest/docker-compose.loadtest.yml run --rm --service-ports loadtest \
    -f locustfile.py --host http://api:8000
```

| Variable | Default | Meaning |
|---|---|---|
| `LOADTEST_ADMIN_EMAIL` / `LOADTEST_ADMIN_PASSWORD` | required | Admin used to create the test product. |
| `LOADTEST_STOCK` | `10` | Units of the limited-stock product. Keep it well below the user count so buyers must compete. |
| `LOADTEST_MAX_P95_MS` | `2000` | Fail the run if overall p95 latency is above this. |
| `LOADTEST_ALLOW_REMOTE` | unset | Set to `1` to allow a non-local `--host` (non-production only). |

## Reading the result

```
CHECKOUT INVARIANT
  initial stock          : 10
  orders created (201)   : 10      <- must equal the units that left stock
  rejected as sold out   : 1444
  idempotent replays ok  : 4
  stock left (from API)  : 0
RESULT: PASS
```

If it says `stock mismatch` or `OVERSOLD`, checkout sold more than existed:
treat it as a bug, not a performance problem.

## Cleaning up

Each run leaves behind one archived product, its category, and the guest
orders. Wipe a dev database with `docker compose down -v`.

Numbers from a laptop running Docker say little about production. Use this to
catch correctness problems under concurrency and large regressions in
latency, not to size servers.
