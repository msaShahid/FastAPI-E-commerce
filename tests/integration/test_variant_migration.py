# ruff: noqa: F811  (the scratch fixture is imported from the migration suite and re-declared as a test parameter)
"""E1 migration proof: legacy data survives untouched, backfills are right and resumable, bad SKUs abort cleanly, the guard SQL matches.

Uses the real alembic command on scratch databases (helpers from test_migrations)."""

import importlib.util
import pathlib
import uuid

import asyncpg
from test_migrations import ROOT, alembic, fetch, scratch  # noqa: F401

BEFORE = "a3e7c1b9d542"       # the revision just before this feature
SCHEMA_REV = "b4d8e2a6c915"


async def seed_legacy(dsn):
    """Pre-feature data: products with stock, a pending order and a cancelled order whose stock was released, a cart, a payment."""
    conn = await asyncpg.connect(dsn)
    try:
        user = uuid.uuid4()
        await conn.execute("INSERT INTO users (id, username, email, password_hash, role, is_active) VALUES ($1, 'legacy', 'legacy@example.com', 'x', 'user', true)", user)
        cat = await conn.fetchval("INSERT INTO categories (name, slug) VALUES ('Legacy', 'legacy') RETURNING id")
        ids = []
        for sku, stock in (("OLD-1", 10), ("OLD-2", 0), ("old-3", 5)):
            ids.append(await conn.fetchval(
                "INSERT INTO products (name, slug, price_cents, sku, stock, status, category_id) VALUES ($1, $2, 1500, $3, $4, 'active', $5) RETURNING id",
                f"Legacy {sku}", f"legacy-{sku.lower()}", sku, stock, cat))
        orders = []
        for key, status, released in (("k-pending", "pending", None), ("k-cancelled", "cancelled", "now()")):
            oid = uuid.uuid4()
            await conn.execute(
                "INSERT INTO orders (id, user_id, status, idempotency_key, subtotal_cents, shipping_cents, tax_cents, total_cents, currency, stock_released_at) "
                f"VALUES ($1, $2, $3, $4, 3000, 100, 200, 3300, 'inr', {released or 'NULL'})", oid, user, status, key)
            for pid, qty in ((ids[0], 2), (ids[2], 1)):
                await conn.execute(
                    "INSERT INTO order_items (order_id, product_id, product_name_snapshot, price_cents_snapshot, quantity) VALUES ($1, $2, $3, 1500, $4)",
                    oid, pid, f"Legacy {pid}", qty)
            orders.append(oid)
        cart = await conn.fetchval("INSERT INTO carts (user_id) VALUES ($1) RETURNING id", user)
        await conn.execute("INSERT INTO cart_items (cart_id, product_id, quantity, price_cents_snapshot) VALUES ($1, $2, 1, 1500)", cart, ids[0])
        return ids, orders, cart
    finally:
        await conn.close()


CHECKSUMS = {
    "orders": "SELECT md5(string_agg(concat_ws('|', id, status, subtotal_cents, shipping_cents, tax_cents, total_cents, currency, created_at, updated_at, stock_released_at), ',' ORDER BY id)) FROM orders",
    "items": "SELECT md5(string_agg(concat_ws('|', id, order_id, product_id, product_name_snapshot, price_cents_snapshot, quantity, created_at, updated_at), ',' ORDER BY id)) FROM order_items",
    "products": "SELECT md5(string_agg(concat_ws('|', id, sku, price_cents, stock, status, created_at, updated_at), ',' ORDER BY id)) FROM products",
    "cart": "SELECT md5(string_agg(concat_ws('|', id, cart_id, product_id, quantity, price_cents_snapshot), ',' ORDER BY id)) FROM cart_items",
}


async def checksums(dsn):
    return {name: (await fetch(dsn, sql))[0][0] for name, sql in CHECKSUMS.items()}


async def test_legacy_data_is_unchanged_and_the_backfills_are_correct(scratch):
    url, dsn = scratch
    alembic(url, "upgrade", BEFORE)
    ids, orders, cart = await seed_legacy(dsn)
    before = await checksums(dsn)

    alembic(url, "upgrade", "head")

    assert await checksums(dsn) == before   # no order, item, product or cart value (amounts, statuses, timestamps included) changed
    items = await fetch(dsn, "SELECT oi.id, oi.sku_snapshot, oi.sku_snapshot_source, oi.restocked_quantity, oi.quantity, p.sku, o.stock_released_at IS NOT NULL AS released "
                             "FROM order_items oi JOIN products p ON p.id = oi.product_id JOIN orders o ON o.id = oi.order_id")
    assert len(items) == 4
    for row in items:
        assert row["sku_snapshot"] == row["sku"] and row["sku_snapshot_source"] == "backfill"
        assert row["restocked_quantity"] == (row["quantity"] if row["released"] else 0)   # only the cancelled (released) order's lines
    assert (await fetch(dsn, "SELECT count(*) FILTER (WHERE kind = 'opening') = count(*) FROM stock_movements"))[0][0] is True  # only opening rows
    assert [tuple(r) for r in await fetch(dsn, "SELECT product_id, delta, after_qty FROM stock_movements ORDER BY product_id")] == [(ids[0], 10, 10), (ids[2], 5, 5)]  # zero stock: no row
    assert [r[0] for r in await fetch(dsn, "SELECT sku_norm FROM sku_registry ORDER BY 1")] == ["OLD-1", "OLD-2", "OLD-3"]   # normalised: 'old-3' -> OLD-3
    assert (await fetch(dsn, "SELECT option_count FROM products GROUP BY 1"))[0][0] == 0                         # every product is still simple
    assert not await fetch(dsn, "SELECT 1 FROM products WHERE price_from_cents <> price_cents OR price_to_cents <> price_cents")  # range back-filled: from = to = price
    assert (await fetch(dsn, "SELECT count(*) FROM pg_indexes WHERE indexname IN ('ix_products_price_from_cents', 'ix_products_price_to_cents')"))[0][0] == 2
    # the cart rule still holds for existing data: no second line for the same product
    conn = await asyncpg.connect(dsn)
    try:
        failed = False
        try:
            await conn.execute("INSERT INTO cart_items (cart_id, product_id, quantity, price_cents_snapshot) VALUES ($1, $2, 1, 1500)", cart, ids[0])
        except asyncpg.UniqueViolationError:
            failed = True
        assert failed
        assert not await conn.fetch("SELECT 1 FROM pg_constraint WHERE conname = 'uq_cart_items_cart_product'")   # replaced by the partial indexes
        assert await conn.fetchval("SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname LIKE 'ux_cart_items_%' AND i.indisvalid") == 2
        assert not await conn.fetch("SELECT 1 FROM pg_constraint WHERE NOT convalidated AND connamespace = 'public'::regnamespace")  # everything validated
    finally:
        await conn.close()


async def test_sku_collisions_that_the_registry_would_reject_abort_the_migration_and_change_nothing(scratch):
    url, dsn = scratch
    alembic(url, "upgrade", BEFORE)
    conn = await asyncpg.connect(dsn)
    try:
        cat = await conn.fetchval("INSERT INTO categories (name, slug) VALUES ('C', 'c') RETURNING id")
        for sku in ("Dup-1", "dup-1"):
            await conn.execute("INSERT INTO products (name, slug, price_cents, sku, stock, status, category_id) VALUES ($1, $2, 100, $3, 1, 'active', $4)", sku, sku, sku, cat)
    finally:
        await conn.close()
    result = alembic(url, "upgrade", "head", expect_ok=False)
    output = result.stdout + result.stderr
    assert result.returncode != 0 and "differ only by letter case" in output and "DUP-1" in output
    assert (await fetch(dsn, "SELECT version_num FROM alembic_version"))[0][0] == BEFORE                       # still at the old revision
    assert not await fetch(dsn, "SELECT 1 FROM information_schema.tables WHERE table_name IN ('product_variants', 'sku_registry', 'stock_movements')")
    assert not await fetch(dsn, "SELECT 1 FROM information_schema.columns WHERE table_name = 'products' AND column_name = 'option_count'")

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("UPDATE products SET sku = 'DUP-2' WHERE sku = 'dup-1'")   # the operator fixes the data ...
    finally:
        await conn.close()
    alembic(url, "upgrade", "head")                                                    # ... and the same command now succeeds
    assert (await fetch(dsn, "SELECT version_num FROM alembic_version"))[0][0] != BEFORE
    assert await fetch(dsn, "SELECT 1 FROM information_schema.tables WHERE table_name = 'product_variants'")


async def test_the_second_revision_can_be_repeated_and_the_first_is_all_or_nothing(scratch):
    url, dsn = scratch
    alembic(url, "upgrade", BEFORE)
    ids, orders, cart = await seed_legacy(dsn)
    alembic(url, "upgrade", SCHEMA_REV)
    # new order lines arrive while only the first revision is applied (the old application is still running)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("INSERT INTO order_items (order_id, product_id, product_name_snapshot, price_cents_snapshot, quantity) VALUES ($1, $2, 'late', 1500, 1)", orders[0], ids[1])
        await conn.execute("UPDATE products SET stock = stock + 1 WHERE id = $1", ids[0])  # even a stock change by old code is recorded
        assert await conn.fetchval("SELECT kind FROM stock_movements WHERE product_id = $1 ORDER BY id DESC LIMIT 1", ids[0]) == "unspecified"
    finally:
        await conn.close()
    alembic(url, "upgrade", "head")
    first = await checksums(dsn)
    alembic(url, "-x", "allow_data_loss=true", "downgrade", SCHEMA_REV)   # (a throwaway database) undo the second revision ...
    alembic(url, "upgrade", "head")            # ... and run it again: the same end state (batches only touch rows not yet done)
    assert await checksums(dsn) == first
    assert not await fetch(dsn, "SELECT 1 FROM order_items WHERE sku_snapshot IS NULL")
    assert (await fetch(dsn, "SELECT count(*) FROM pg_indexes WHERE indexname LIKE 'ux_cart_items_%'"))[0][0] == 2


def _load(path):
    spec = importlib.util.spec_from_file_location("migration_module", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_and_the_models_install_exactly_the_same_database_rules():
    from app.modules.products.models import variant_guard

    migration = _load(pathlib.Path(ROOT) / "alembic" / "versions" / "b4d8e2a6c915_product_variants_schema.py")
    assert tuple(migration.FUNCTIONS) == tuple(variant_guard.FUNCTIONS)
    assert tuple(migration.TRIGGERS) == tuple(variant_guard.TRIGGERS)
    assert set(migration.TRIGGER_TABLES) == {name for name, _sql in variant_guard.TRIGGERS}


async def test_a_limited_application_role_cannot_forge_alter_or_bypass_the_stock_ledger(scratch):
    """The deployment recipe in docs/VARIANTS.md: the application connects as a role that owns nothing. Emulated with SET ROLE on a
    superuser connection (which drops superuser rights for the session)."""
    url, dsn = scratch
    alembic(url, "upgrade", "head")
    conn = await asyncpg.connect(dsn)
    try:
        role = f"novacart_app_probe_{uuid.uuid4().hex[:8]}"  # roles are cluster-wide: never collide with another run
        await conn.execute(f"CREATE ROLE {role} NOLOGIN")
        await conn.execute(f"GRANT USAGE ON SCHEMA public TO {role}")
        await conn.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}")
        await conn.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}")
        await conn.execute(f"REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON stock_movements FROM {role}")
        cat = await conn.fetchval("INSERT INTO categories (name, slug) VALUES ('R', 'r') RETURNING id")
        pid = await conn.fetchval("INSERT INTO products (name, slug, price_cents, sku, stock, status, category_id) VALUES ('R', 'r', 100, 'ROLE-1', 5, 'active', $1) RETURNING id", cat)
        await conn.execute(f"SET ROLE {role}")

        async def denied(sql):
            try:
                await conn.execute(sql)
            except (asyncpg.InsufficientPrivilegeError, asyncpg.IntegrityConstraintViolationError, asyncpg.DataError):
                return True
            return False

        # what the application legitimately does still works and is still recorded (the writer runs with its owner's rights)
        await conn.execute("SELECT set_config('novacart.stock_kind', 'sale', false)")
        await conn.execute(f"UPDATE products SET stock = stock - 1 WHERE id = {pid}")
        await conn.execute("SELECT set_config('novacart.stock_kind', '', false)")
        assert await conn.fetchval(f"SELECT after_qty FROM stock_movements WHERE product_id = {pid} ORDER BY id DESC LIMIT 1") == 4
        # what a bug or an attacker with only that role could try
        assert await denied(f"INSERT INTO stock_movements (kind, product_id, delta, before_qty, after_qty) VALUES ('sale', {pid}, -1, 4, 3)")
        assert await denied("UPDATE stock_movements SET delta = delta")
        assert await denied("DELETE FROM stock_movements")
        assert await denied("TRUNCATE stock_movements")
        assert await denied("ALTER TABLE stock_movements DISABLE TRIGGER USER")
        assert await denied("SET session_replication_role = replica")           # would switch all triggers off
        assert await denied("DROP TRIGGER trg_products_stock_ledger_upd ON products")
        await conn.execute("RESET ROLE")
    finally:
        await conn.execute("RESET ROLE")
        await conn.execute(f"DROP OWNED BY {role}")
        await conn.execute(f"DROP ROLE IF EXISTS {role}")
        await conn.close()


def test_the_price_range_migration_and_the_models_install_exactly_the_same_database_rules():
    from app.modules.products.models import price_guard

    migration = _load(pathlib.Path(ROOT) / "alembic" / "versions" / "d2f6b9a4c731_product_price_range.py")
    assert tuple(migration.FUNCTIONS) == tuple(price_guard.FUNCTIONS)
    assert tuple(migration.TRIGGERS) == tuple(price_guard.TRIGGERS)


def test_the_invoice_migration_and_the_models_install_exactly_the_same_immutability_rule():
    from app.modules.invoices.models import guard

    migration = _load(pathlib.Path(ROOT) / "alembic" / "versions" / "f1c4d8e2a937_invoices_and_business_profile.py")
    assert migration.FUNCTION == guard.FUNCTION and migration.TRIGGER == guard.TRIGGER
