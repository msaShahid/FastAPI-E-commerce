# Index audit

Stage 4, item 1. The goal: find the queries that will get slow as data grows,
while it is still cheap to fix them.

## Method

1. Create a separate database named `*_audit` and load a realistic data set
   (about 2.5 million rows: 300k orders, 600k order items, 200k products,
   200k audit-log rows, 100k users, and so on).
2. Run each of the application's real repository methods, capture the exact
   SQL they send, and run `EXPLAIN (ANALYZE)` on it.
3. Look for **Seq Scan** on a large table. That means Postgres read every row
   because no index could help.

The tool is `scripts/index_audit.py` (see its docstring for the exact
commands). `--check` exits non-zero if any query other than a `count(*)` scans
a large table, so it is also a regression guard: if someone drops an index or
adds a query with no index, it fails.

## What was found

Before, 12 queries sequentially scanned a large table. The worst offenders (time
for the query that returns one page of rows, 2.5M-row database):

| Query | Before | After |
|---|---|---|
| orders: admin "all orders" | 65.4 ms | 0.1 ms |
| products: search `camera` | 56.0 ms | 0.3 ms |
| products: page 2001 (offset 40000) | 54.9 ms | 15.3 ms |
| orders: admin, status = paid | 30.2 ms | 0.1 ms |
| units sold for one product | 25.1 ms | 0.0 ms |
| products: storefront list | 24.0 ms | 0.1 ms |
| foreign-key check: `order_items.product_id` | 21.8 ms | 0.0 ms |
| products: admin list (all statuses) | 21.7 ms | 0.0 ms |
| audit log: newest | 20.1 ms | 0.0 ms |
| orders: stale-pending reaper | 18.4 ms | 5.4 ms |
| audit log: filtered by action | 11.2 ms | 0.1 ms |
| users: admin list | 10.7 ms | 0.0 ms |

These timings are small because the sandbox is fast and the data fits in
memory. The point is the **shape of the plan**: a sequential scan grows with
the table, an index scan does not. As a rule of thumb, with 10x the data the
"before" numbers grow roughly in proportion, while the "after" numbers barely
move.

Queries by id, email, token, user, idempotency key, payment id and refresh-token
id were already index scans and were left alone.

## Changes (migration `f3b8a6c2d915`)

| Index | Serves |
|---|---|
| `products (status, created_at)` | storefront listing, newest first |
| `products (created_at)` | admin product list (all statuses) |
| `products (category_id, status, created_at)` | same, filtered by category. **Replaces** `ix_products_category_id` |
| `products (name)` GIN `gin_trgm_ops` | search: `name ILIKE '%term%'` (needs `pg_trgm`) |
| `orders (user_id, created_at)` | "my orders". **Replaces** `ix_orders_user_id` |
| `orders (created_at)` | admin "all orders" |
| `orders (status, created_at)` | admin status filter, and the stale-order reaper |
| `order_items (product_id)` | foreign key to products; checked on every product delete |
| `cart_items (product_id)` | same |
| `audit_logs (created_at)` | audit list, newest first |
| `users (created_at)` | admin user list |

Two old single-column indexes were removed because a new composite index
starts with the same column and serves the same lookups.

Costs to know about: every index makes writes to its table slightly slower and
uses disk. These tables are read far more than written. Checkout's inserts
gain the new `orders`, `order_items` and `cart_items` indexes; its stock updates
touch none of them, because `stock` is not in any index.

## What indexes cannot fix

* **`count(*)` for the pagination total.** Counting 170,000 matching rows takes
  about 20 ms with or without an index; it has to visit them all. This is the
  main remaining cost of a list endpoint. Caching the total (item 4.3) is the
  practical fix.
* **Deep pages.** `OFFSET 40000` still reads and discards 40,000 index entries
  (15 ms here). Fine for the first pages people actually view; if deep paging
  matters, switch to keyset pagination ("the 20 rows after this `created_at`").
* **Search terms shorter than 3 characters** cannot use a trigram index. They
  fall back to the `created_at` index and stay fast when many rows match, but a
  2-letter term that matches almost nothing would scan.

## Large production tables

The migration uses plain `CREATE INDEX`, which blocks writes to a table while
it builds. That is instant at today's size. On a large, busy table, first build
the index yourself with `CREATE INDEX CONCURRENTLY <same name> ON ...`; the
migration is written with `IF NOT EXISTS`, so it then skips it.

## Reading EXPLAIN ANALYZE

```
Index Scan Backward using ix_products_status_created_at on products
    (cost=0.42..14916.25 rows=169347 width=629)
    (actual time=0.020..0.090 rows=20 loops=1)
Execution Time: 0.116 ms
```

* **Seq Scan**: reads the whole table. Fine for tiny tables, a problem for big
  ones.
* **Index Scan**: uses an index to find rows, then reads them from the table.
* **Index Only Scan**: the index alone had everything needed.
* **Bitmap Index/Heap Scan**: collects matching row locations from an index,
  then reads them in table order. Typical when a medium share of rows matches.
* **cost=a..b**: the planner's *estimate*, in arbitrary units. Only useful for
  comparing plans for the same query, not as a time.
* **actual time / rows**: what really happened. Compare `rows=` estimated vs.
  actual; a big mismatch usually means stale statistics (`ANALYZE`).
* `LIMIT 20` plus a matching index is why the plan above stops after 20 rows
  instead of touching all 169,347.
