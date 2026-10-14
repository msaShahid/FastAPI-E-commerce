"""
Index audit: run the app's real queries against a large database and show
EXPLAIN ANALYZE for each, so missing indexes show up as sequential scans.

SAFETY: this script bulk-loads ~1.7 million rows, so it refuses to run unless
the database name ends in "_audit". Never point it at your development or
production database. Create a separate one:

    docker compose exec db createdb -U <POSTGRES_USER> shop_audit

Then run everything against that database by overriding DATABASE_URL:

    URL=postgresql+asyncpg://<user>:<password>@db:5432/shop_audit
    docker compose exec -e DATABASE_URL=$URL api alembic upgrade head
    docker compose exec -e DATABASE_URL=$URL api python -m scripts.index_audit --seed
    docker compose exec -e DATABASE_URL=$URL api python -m scripts.index_audit
    docker compose exec -e DATABASE_URL=$URL api python -m scripts.index_audit --check   # exit 1 on a seq scan

How to read the output (see docs/index-audit.md for more):

  * "page"   is the query that returns one page of rows. It should be an
             Index Scan. A Seq Scan on a large table means an index is missing.
  * "count"  is the SELECT count(*) that feeds the pagination total. It has to
             visit every matching row, so it is expected to scan; it is
             reported but never fails --check.
  * "lookup" is a single-row / foreign-key style query. Should be an index scan.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.modules.addresses.repositories.address_repository import AddressRepository
from app.modules.audit.repositories.audit_repository import AuditRepository
from app.modules.auth.repositories.auth_repository import AuthRepository
from app.modules.cart.repositories.cart_repository import CartRepository
from app.modules.categories.repositories.category_repository import CategoryRepository
from app.modules.orders.repositories.order_repository import OrderRepository
from app.modules.payments.repositories.payment_repository import PaymentRepository
from app.modules.products.repositories.product_repository import ProductRepository
from app.modules.users.repositories.user_repository import UserRepository
from app.shared.enums.order_status import OrderStatus

SEED_FILE = Path(__file__).with_name("index_audit_seed.sql")
BIG_TABLE_ROWS = 20_000  # a seq scan over fewer rows than this is not a concern


def _check_database_name(url: str) -> None:
    name = make_url(url).database or ""
    if not name.endswith("_audit"):
        sys.exit(
            f"Refusing to run: database {name!r} does not end in '_audit'.\n"
            "This script loads ~1.7M rows; use a dedicated database (see the docstring)."
        )


def _walk(node: dict, out: list[dict]) -> None:
    out.append(node)
    for child in node.get("Plans", []):
        _walk(child, out)


async def _seed(engine) -> None:
    async with engine.connect() as conn:
        users = (await conn.execute(text("SELECT count(*) FROM users"))).scalar_one()
    if users:
        sys.exit("Database already has data; seed only an empty, freshly migrated database.")
    sql = SEED_FILE.read_text()
    print("Seeding (about a minute)...")
    async with engine.connect() as conn:
        raw = await conn.get_raw_connection()
        await raw.driver_connection.execute(sql)
    print("Done.")


async def _explain(engine, statement: str, params) -> tuple[float, list[str], list[str]]:
    async with engine.connect() as conn:
        result = await conn.exec_driver_sql(
            "EXPLAIN (ANALYZE, FORMAT JSON) " + statement, params
        )
        document = result.scalar()
    document = document if isinstance(document, list) else json.loads(document)
    plan = document[0]
    nodes: list[dict] = []
    _walk(plan["Plan"], nodes)

    scans, big_seq = [], []
    for node in nodes:
        node_type = node["Node Type"]
        if "Scan" not in node_type:
            continue
        index = f"/{node['Index Name']}" if "Index Name" in node else ""
        scans.append(f"{node_type}[{node.get('Relation Name')}{index}]")
        if node_type == "Seq Scan":
            rows = node.get("Actual Rows", 0) * node.get("Actual Loops", 1)
            rows += node.get("Rows Removed by Filter", 0)
            if rows > BIG_TABLE_ROWS:
                big_seq.append(node["Relation Name"])
    return plan["Execution Time"], scans, big_seq


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--seed", action="store_true", help="load the audit data set, then exit")
    parser.add_argument("--check", action="store_true", help="exit 1 if a non-count query seq-scans a big table")
    args = parser.parse_args()

    url = get_settings().database_url
    _check_database_name(url)
    engine = create_async_engine(url)
    if args.seed:
        await _seed(engine)
        await engine.dispose()
        return 0

    captured: list[tuple[str, tuple]] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            captured.append((statement, parameters))

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as s:
        try:
            user_id = (await s.execute(text(
                "SELECT user_id FROM orders WHERE user_id IS NOT NULL OFFSET 12345 LIMIT 1"))).scalar()
        except ProgrammingError:
            sys.exit("Tables not found. Run `alembic upgrade head` against this database first.")
        if user_id is None:
            sys.exit("No data found. Run with --seed first.")
        order_id = (await s.execute(text("SELECT id FROM orders WHERE user_id=:u LIMIT 1"), {"u": user_id})).scalar()
        product_id = (await s.execute(text("SELECT id FROM products OFFSET 777 LIMIT 1"))).scalar()
        guest_token = (await s.execute(text("SELECT guest_token FROM orders WHERE guest_token IS NOT NULL LIMIT 1"))).scalar()
        jti = (await s.execute(text("SELECT jti FROM refresh_tokens LIMIT 1"))).scalar()
        family_id = (await s.execute(text("SELECT family_id FROM refresh_tokens LIMIT 1"))).scalar()
        provider_id = (await s.execute(text("SELECT provider_payment_id FROM payments LIMIT 1"))).scalar()

    def products(s: AsyncSession) -> ProductRepository:
        return ProductRepository(s)

    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=30)
    scenarios = {
        "products: storefront list": lambda s: products(s).list_paginated(offset=0, limit=20),
        "products: deep page (offset 40000)": lambda s: products(s).list_paginated(offset=40000, limit=20),
        "products: by category, newest": lambda s: products(s).list_paginated(offset=0, limit=20, category_id=7),
        "products: by category, sort price": lambda s: products(s).list_paginated(offset=0, limit=20, category_id=7, sort="price_cents"),
        "products: price range, sort price": lambda s: products(s).list_paginated(offset=0, limit=20, min_price=1000, max_price=5000, sort="price_cents"),
        "products: sort by name": lambda s: products(s).list_paginated(offset=0, limit=20, sort="name"),
        "products: search 'camera'": lambda s: products(s).list_paginated(offset=0, limit=20, search="camera"),
        "products: admin list (all statuses)": lambda s: products(s).list_paginated(offset=0, limit=20, include_inactive=True),
        "products: by id": lambda s: products(s).get_by_id(product_id),
        "orders: my orders": lambda s: OrderRepository(s).list_for_user(user_id, offset=0, limit=20),
        "orders: admin all orders": lambda s: OrderRepository(s).list_all(offset=0, limit=20),
        "orders: admin, status=paid": lambda s: OrderRepository(s).list_all(offset=0, limit=20, status=OrderStatus.PAID),
        "orders: by id (+items)": lambda s: OrderRepository(s).get_by_id(order_id),
        "orders: idempotency (user)": lambda s: OrderRepository(s).get_by_idempotency_key("key-1", user_id=user_id),
        "orders: idempotency (guest)": lambda s: OrderRepository(s).get_by_idempotency_key("key-9", guest_token=guest_token),
        "orders: stale pending (reaper)": lambda s: OrderRepository(s).list_stale_pending_order_ids(older_than=cutoff),
        "payments: by order": lambda s: PaymentRepository(s).list_for_order(order_id),
        "payments: by provider id": lambda s: PaymentRepository(s).get_by_provider_payment_id(provider_id),
        "audit: newest": lambda s: AuditRepository(s).list_paginated(offset=0, limit=20),
        "audit: by action": lambda s: AuditRepository(s).list_paginated(offset=0, limit=20, action="product.created"),
        "audit: by actor": lambda s: AuditRepository(s).list_paginated(offset=0, limit=20, actor_user_id=user_id),
        "users: admin list": lambda s: UserRepository(s).list_paginated(offset=0, limit=20),
        "users: by email": lambda s: AuthRepository(s).get_user_by_email("user4242@example.com"),
        "categories: list": lambda s: CategoryRepository(s).list_paginated(offset=0, limit=20),
        "addresses: by user": lambda s: AddressRepository(s).list_for_user(user_id),
        "auth: refresh token by jti": lambda s: AuthRepository(s).get_refresh_token_by_jti(jti),
        "auth: revoke family": lambda s: AuthRepository(s).revoke_family(family_id),
        "cart: by user": lambda s: CartRepository(s).get_cart_by_user_id(user_id),
        "cart: by guest token": lambda s: CartRepository(s).get_cart_by_guest_token("nope"),
    }
    raw_queries = {
        "FK: order_items by product_id": ("SELECT 1 FROM order_items WHERE product_id = $1 LIMIT 1", (product_id,)),
        "FK: cart_items by product_id": ("SELECT 1 FROM cart_items WHERE product_id = $1 LIMIT 1", (product_id,)),
        "FK: payments by order_id": ("SELECT 1 FROM payments WHERE order_id = $1", (order_id,)),
        "FK: orders by user_id": ("SELECT 1 FROM orders WHERE user_id = $1 LIMIT 1", (user_id,)),
        "units sold for one product": ("SELECT sum(quantity) FROM order_items WHERE product_id = $1", (product_id,)),
    }

    problems: list[str] = []
    print(f"{'scenario':36} {'kind':6} {'ms':>7}  plan")

    async def report(name: str, statement: str, params) -> None:
        kind = "count" if "count(" in statement.lower() else ("page" if "LIMIT" in statement.upper() and "OFFSET" in statement.upper() or "ORDER BY" in statement.upper() else "lookup")
        await _explain(engine, statement, params)  # warm the cache
        ms, scans, big_seq = await _explain(engine, statement, params)
        flag = ""
        if big_seq and kind != "count":
            flag = "  <<< SEQ SCAN"
            problems.append(f"{name}: sequential scan of {', '.join(sorted(set(big_seq)))}")
        print(f"{name[:36]:36} {kind:6} {ms:7.1f}  {', '.join(scans)}{flag}")

    for name, run in scenarios.items():
        captured.clear()
        async with session_factory() as s:
            await run(s)
            await s.rollback()
        for statement, params in list(captured):
            await report(name, statement, params)
    for name, (statement, params) in raw_queries.items():
        await report(name, statement, params)
    await engine.dispose()

    print()
    if problems:
        print("Sequential scans on large tables:")
        for p in problems:
            print("  -", p)
    else:
        print("No query (other than count totals) sequentially scans a large table.")
    return 1 if (problems and args.check) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
