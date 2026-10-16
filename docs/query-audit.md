# N+1 query audit

An N+1 is when returning N rows costs about N extra SQL statements, usually
one per row, instead of a constant few. It is invisible with 3 rows in
development and slow with 3,000 in production.

## How it was checked

`tests/integration/test_query_counts.py` runs each endpoint against a real
Postgres twice, once returning 2 rows and once 25, counts the SQL statements
that actually reach the database (a SQLAlchemy `before_cursor_execute`
listener), and asserts the two counts are **equal** and under a small ceiling.
If a change makes the large case send even one extra statement per row, the
test fails and says so. Run it with `-s` to see the counts:

```bash
docker compose exec api pytest tests/integration/test_query_counts.py -s
SHOW_SQL=1 docker compose exec api pytest tests/integration/test_query_counts.py -s -k checkout   # prints every statement
```

The counts include authentication (one user lookup), because that is part of
what a request costs.

## Results

Statements per request, the same for 2 rows and for 25:

| Request | Statements | What they are |
|---|---|---|
| `GET /products` | 3 | count, page, categories (`selectinload`) |
| `GET /products` (admin) | 4 | + user lookup |
| `GET /categories` | 2 | count, page |
| `GET /orders` | 4 | user, count, orders, items (`selectinload`) |
| `GET /orders/admin` | 4 | same |
| `GET /cart` | 4 | user, cart, items, products |
| `GET /audit-logs` | 3 | user, count, page |
| `GET /addresses` | 2 | user, list |
| `GET /users` | 3 | user, count, page |
| GraphQL `users(pageSize: n)` | 3 | user, count, page |
| `POST /orders/checkout` | 13 | **was 2 per cart line** (see below) |
| `POST /auth/login` merging a guest cart | 13 | **was 3 per guest-cart line** (see below) |
| `cancel_stale_orders`, per order | constant | the same for a 1-line and a 10-line order |

## Two real N+1s, now fixed

**1. Checkout, 2 statements per cart line.** It locked each product with its
own `SELECT ... FOR UPDATE` and updated each product's stock with its own
`UPDATE`. A 2-line cart sent 15 statements, a 25-line cart 61. Now:
* one `SELECT ... WHERE id IN (...) ORDER BY id FOR UPDATE` locks every product.
  `ORDER BY id` makes Postgres take the locks in ascending id order, the same
  global order as before, so concurrent checkouts still cannot deadlock;
* one `UPDATE ... SET stock = stock + CASE id WHEN ... END` changes all the
  stock (`ProductRepository.adjust_stock_many`).

  Why not just let the ORM flush? Because the ORM sends one `UPDATE` per
  changed object here: it also reads back `updated_at` (`eager_defaults` in
  `TimestampMixin`), which turns off batching.

13 statements, whether the cart has 2 lines or 25. The stale-order job
(`cancel_stale_orders`) restocks the same way.

**2. Logging in with a guest cart, 3 statements per guest line.** Merging the
guest cart into the account cart looked up each product in the account cart
with its own query, then inserted and re-read each new line. 16 statements for
2 lines, 85 for 25. `CartRepository.merge_items_into_cart` now works from the
account cart's already-loaded lines and flushes once: 13 either way.

## Not a problem here (and why)

* **Lazy loading cannot silently N+1 in this app.** With async SQLAlchemy,
  touching a relationship that was not loaded raises `MissingGreenlet` (an
  HTTP 500) instead of quietly issuing a query. That is how the earlier
  product-create and first-add-to-cart bugs surfaced. So an N+1 here comes from
  an explicit `await` inside a loop, which is what the two above were.
* **GraphQL has only user queries, and `UserType` has no related rows**, so
  there is nothing to N+1 today. The GraphQL test is a guard for the day a
  field such as `orders` is added.
* **`cancel_stale_orders` grows with the number of orders**, on purpose: each
  order is locked and cancelled in its own transaction so one contested order
  never blocks the rest. It is a scheduled job, not a request. What must not
  grow is the cost of an order with more lines, and a test pins that.

## Rules for new code

1. A relationship you serialise must be loaded up front with
   `selectinload(...)` in the repository query (`joinedload` for many-to-one if
   you prefer a join). One extra query for the whole page, not one per row.
2. No `await` that hits the database inside a loop over rows. Fetch with
   `WHERE id IN (...)`, change in memory, flush once. If a bulk `UPDATE` is
   needed, see `adjust_stock_many`.
3. If you add a GraphQL field that returns related rows (say `UserType.orders`),
   do not query inside the resolver per user: load them for the whole page
   with a Strawberry `DataLoader` (one `WHERE user_id IN (...)` query), and add
   the query to `test_query_counts.py`.
4. Add the endpoint to `test_query_counts.py`: seed 25 rows, request 2 and 25,
   call `assert_constant`.

## Bulk writes and the cache

A bulk `update(Product)...` never goes through the ORM flush, so the cache's
flush hook (see `docs/caching.md`) cannot see it. `app/core/cache.py` therefore
also listens for bulk statements (`do_orm_execute`) on the products and
categories tables. Without that, the bulk stock update above would leave stale
stock in the cached product list; `test_a_real_checkout_invalidates_the_list`
covers it.
