# Product variants: database, stock ledger, pricing and deployment (milestones E1 to E4)

Status: E1 delivered the **database foundation**, E2 the **pricing and availability** reads, E3 the **cart**, E4 **checkout and restocking**. No API creates variants yet (E5). What changes for users today is
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

## E2: prices, ranges, filtering and public availability

Migration `d2f6b9a4c731` (after E1's two): adds `products.price_from_cents` / `price_to_cents`, kept current by database triggers, and two
indexes. Added `NOT NULL DEFAULT 0` (no rewrite), indexes built `CONCURRENTLY`, existing products back-filled in batches of 5,000 (resumable,
changes no price/stock/status/timestamp), constraint validated last. Run order and recovery are as for E1 above.

- **Effective price** = the variant's override if it has one, else the product's price. A product with options shows the lowest and
  highest effective price over its **active variants, in stock or not**; with no active variant it falls back to its own price.
- **Price filters** (`min_price`, `max_price`): a product with options matches if an ACTIVE variant's effective price is inside the range
  (exact, not a range overlap). Simple products match on their price, as before.
- **Sorting**: `sort=price_cents` is by lowest price, `sort=-price_cents` by highest; ties keep a stable order.
- **Public stock**: `stock` is `null` for everyone but administrators; the public gets `available` and `availability`
  (`in_stock`, `low_stock`, `out_of_stock`; low = at or below `LOW_STOCK_DISPLAY_THRESHOLD`, default 5). A product with options shows its
  best active variant's availability. `PUBLIC_STOCK_VISIBILITY=exact` shows counts publicly instead.
- **Responses**: new `has_variants`, `variant_count`, `price_from_cents`, `price_to_cents`, `has_price_range`, `available`,
  `availability`, and (administrators) `base_price_cents`. For a product with options `price_cents` is the LOWEST effective price.
  Detail responses add `options` and `variants`; the public sees active variants only.
- **Cache**: variant, option and option-value changes invalidate the cached product lists; the cache key includes the stock-visibility
  setting; administrators are never served the cached public view.
- A variant that has ever held stock cannot be deleted (the ledger references it): deactivate it instead.

## E3: the cart

No migration. `POST /cart/items` takes `variant_id` (and `product_id`, which must then match); the price is never sent: the backend uses the
current effective price and stores it as the line's snapshot.

- A product with options can only be added through one of its ACTIVE variants: `422 variant_required` if only the product is sent,
  `422 variant_unavailable` for every other way a variant cannot be bought (unknown, another product's, inactive, product not on sale).
  Simple products behave exactly as before.
- Stock is checked against the variant (including what the cart already holds of it). Exact stock is not public, so the stock error names
  a number only when stock is at or below `LOW_STOCK_DISPLAY_THRESHOLD`; otherwise it says "Not enough stock for that quantity".
- One cart line per product (simple) or per variant; guest carts merge into the user's cart by `(product, variant)`, summing quantities.
- Each line in `GET /cart` now has `variant` (id, sku, options), `current_price_cents` (the effective price now), `price_changed` (snapshot
  differs from that), and `available` / `unavailable_reason` (`unavailable`, `out_of_stock`, `insufficient_stock`). A line that can no longer
  be bought does not break the cart. The cart also returns `cart_digest`: a fingerprint of products, variants and quantities (not prices),
  independent of line order. Clients may ignore it; checkout will use it in E4.

## E4: checkout, order lines and restocking

No migration. Checkout now sells variants.

- **Authority.** Checkout re-reads everything from rows it has LOCKED: products first (ascending id), then the variants in the cart
  (ascending id): the same order in checkout and restock, so concurrent buyers cannot deadlock. Price is the variant's override or the
  product's price from those rows, never the cart's snapshot or anything the client sent; stock is checked and moved on the unit sold
  (the variant, or the simple product). A changed price returns 409 `price_changed` (now naming `variant_id` and `sku`), refreshes the
  cart and takes nothing; checking out again charges the new price. A variant that is inactive, whose product is not on sale, or that has
  too little stock refuses the whole checkout and changes nothing.
- **Order lines are frozen.** Each line stores the SKU (the variant's), the variant id and a `variant_snapshot` (SKU, selected options as
  words, whether the price came from an override or the product, the product price and override at that moment). Later renames, price
  edits or deactivation never change them (the database also refuses edits to order lines). `GET /orders/...` shows `variant_id`, `sku`
  and `variant.options` per item; orders without variants show `variant: null` as before.
- **Ledger.** Sale rows now name the order (`stock_movements.order_id`), for products and variants.
- **Replay protection.** An order stores `cart_digest` (products, variants, quantities, not prices). The same idempotency key with the same
  address is answered as before: an empty cart (the normal retry) returns the original order, and so does a cart holding the same lines. A
  NON-empty cart with DIFFERENT lines under a reused key is refused with the existing `409 idempotency_key_reused` and left untouched.
  A different address is refused as before. Orders made before this change have no digest and compare on the address alone, as before.
  Simultaneous requests with one key still make one order (the per-key lock is unchanged).
- **Restocking is per order line.** Cancelling an unpaid order, the expiry sweep, and a provider-confirmed cancel-with-refund of a paid,
  unshipped order put each line back: to its VARIANT if it has one. Each line is recorded once as a restock event
  (`order_item_restocks`, unique per line and event); a retry finds it recorded and adds nothing, and the database refuses to restock a
  line beyond what was bought. A full refund of a SHIPPED order does not restock (unchanged policy); external partial refunds never touch
  stock. `orders.stock_released_at` still marks whole-order release; the database does not additionally force it to equal "every line fully
  restocked" because existing guard tests set that flag by hand.
- A line bought as a simple product whose product has since gained options cannot be restocked (there is no variant to return it to); the
  conversion operation (E5) refuses while any order that could still be restocked exists, so this guard should never fire.
