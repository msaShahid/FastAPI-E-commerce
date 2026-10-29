# Product variants: database, stock ledger and deployment (milestone E1)

Status: E1 delivers the **database foundation** only. No API creates or sells variants yet (E2 to E5). What changes for users today is
listed under "What E1 changes". Design baseline: Product Variants Design v2 plus the final instructions.

## What E1 adds

- `products.option_count` (0 = a simple product, exactly as before; 1..3 = sold only through variants).
- `product_options`, `product_option_values`, `product_variants`: fixed option slots. Composite foreign keys tie every option, value and
  variant to ONE product and every value to its option's slot; one unique index forbids duplicate combinations; CHECKs keep stock
  and price valid; a product with options keeps no stock of its own.
- `sku_registry`: every product and variant SKU, upper-cased and trimmed. Its primary key makes SKUs unique across products AND variants,
  case-insensitively, including under concurrent writes. SKUs can no longer be changed after creation (enforced by the database).
- `stock_movements`: the stock ledger (below). `order_item_restocks` and `order_items.restocked_quantity`: per-line restock events
  (used from E4). `order_items.variant_id / sku_snapshot / sku_snapshot_source / variant_snapshot`, `cart_items.variant_id`,
  `orders.cart_digest`: nullable columns used by later milestones. New order lines already record their SKU (`sku_snapshot_source = 'order'`).
- Order-line history is protected: price, quantity, name, product and variant of an order line cannot be changed after it is created.

## What E1 changes for users

- `PATCH /products/{id}` with `stock` now **requires `stock_reason`** (3 to 200 characters), recorded with the administrator's id.
  Without it the answer is 422 `stock_reason_required`. Other product edits are unchanged.
- Nothing else in the public API changes in E1.

## The stock ledger

Every change of a product's (and, later, a variant's) stock writes one row to `stock_movements` with `before_qty`, `after_qty`, `delta`,
`kind` (`opening`, `initial`, `sale`, `restock`, `admin_set`, `admin_delta`, `conversion`, `unspecified`), actor, reason and order.

- Written by database triggers in the SAME transaction as the stock change: they commit or roll back together, and no code path can
  change stock unrecorded. Concurrent changes serialise on the row, so each row's `before_qty` equals the previous row's `after_qty`.
- Append-only: the database refuses UPDATE, DELETE and any INSERT not made by the stock triggers. An administrator's change must carry an
  actor and a reason, or the database refuses it.
- `unspecified` means stock was changed without the application's context: during the rollout window old code does that (section below);
  afterwards it means someone changed stock directly in the database.
- Check: for every product, `sum(delta)` equals `stock` (opening balances make this true from the migration onwards).

### Who can bypass it (read this)

The triggers stop ordinary application code and ordinary SQL from forging or editing the ledger. A **database owner or superuser can
still disable triggers or TRUNCATE**. The shipped `docker-compose.prod.yml` runs the application as the database's own (superuser) role, so
with that setup the guarantee is against application bugs, not against someone holding the database password.
To close that gap, run the application as a limited role (a compose change is also needed so the `migrate` service keeps the owner role):

```sql
-- once, as the owner/superuser, AFTER the migrations; replace the names and the password
CREATE ROLE novacart_app LOGIN PASSWORD 'CHANGE_ME';
GRANT CONNECT ON DATABASE novacart TO novacart_app;
GRANT USAGE ON SCHEMA public TO novacart_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO novacart_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO novacart_app;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON stock_movements FROM novacart_app;
```

The ledger writer runs with its owner's rights (`SECURITY DEFINER`), so the application still records stock changes. A test
(`test_a_limited_application_role_cannot_forge_alter_or_bypass_the_stock_ledger`) shows that, with such a role, direct INSERT, UPDATE,
DELETE, TRUNCATE, `DISABLE TRIGGER`, `session_replication_role = replica` and `DROP TRIGGER` on the ledger are all refused.

## Deployment sequence

Needs **PostgreSQL 15 or newer** (`NULLS NOT DISTINCT`). The first migration refuses older servers and changes nothing.

1. Back up the database. Try both revisions on a copy first and note how long they take.
2. Pre-flight (the migration repeats these checks and aborts without changing anything if they fail):
   ```sql
   SELECT upper(btrim(sku)), count(*), string_agg(id::text, ', ') FROM products GROUP BY 1 HAVING count(*) > 1;  -- must return no rows
   SELECT id, sku FROM products WHERE sku <> btrim(sku) OR btrim(sku) = '';                                      -- must return no rows
   ```
3. Run the migrations in a quiet period (`alembic upgrade head`; the `migrate` profile in production compose):
   - `b4d8e2a6c915` (one transaction, 3-second lock timeout): new tables, nullable columns, database rules, SKU registry and opening
     ledger balances. Existing rows and the running application are unaffected.
   - `c7f1a9d3e526` (resumable): builds the cart indexes `CONCURRENTLY`, back-fills order items in batches of 5,000, then validates the
     new constraints. No order amount, quantity, status or timestamp is changed.
4. Deploy the new application code. (Old code keeps working against the new schema; its stock changes are recorded as `unspecified`.)
5. Verify: `SELECT count(*) FROM stock_movements WHERE kind = 'unspecified'` stops growing after the deploy; the sum check above holds.

## If something is interrupted

- `b4d8e2a6c915` is all-or-nothing. If it fails, nothing changed: fix the cause (for example colliding SKUs) and run it again.
- `c7f1a9d3e526` can simply be run again: it drops an INVALID index left by an interrupted `CONCURRENTLY` build, recreates it, and its
  back-fills only touch rows not yet done. Check for leftovers with
  `SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE NOT i.indisvalid;`.
- Downgrades are refused on a database with data (`docs/MIGRATIONS.md`). To undo in production, restore the backup.

## Known limits of E1

- The `sku_snapshot` of order lines created before E1 is filled from today's product SKU and marked `backfill`: application code never
  changed a SKU, but the database did not forbid it, so this cannot be proven historical for hand-edited data.
- Sales rows in the ledger do not yet carry the order id (checkout creates the order after reserving stock); E4 attaches it.
- The unit tests' fake repositories have no database session, so for them the ledger context is a no-op.
