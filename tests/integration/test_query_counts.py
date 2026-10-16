"""
N+1 guard: the number of SQL statements an endpoint sends must not grow with
the amount of data it returns.

Each test measures the same endpoint with a small and a large result (2 rows
vs 25 rows) against a real Postgres, counts the statements that actually reach
the database, and asserts the two counts are EQUAL (and under a small ceiling).
An N+1 -- one extra query per row -- makes the large case send ~23 more.

The counts include authentication (one user lookup) because that is part of
what the request really costs.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest_asyncio
from sqlalchemy import delete, event, select, update

from app.core.security import create_access_token
from app.modules.addresses.models.address import Address
from app.modules.audit.models.audit_log import AuditLog
from app.modules.auth.models.user import User
from app.modules.cart.models.cart import Cart
from app.modules.cart.models.cart_item import CartItem
from app.modules.cart.repositories.cart_repository import CartRepository
from app.modules.categories.models.category import Category
from app.modules.orders.models.order import Order
from app.modules.orders.models.order_item import OrderItem
from app.modules.orders.repositories.order_repository import OrderRepository
from app.modules.orders.services.order_service import OrderService
from app.modules.products.models.product import Product
from app.modules.products.repositories.product_repository import ProductRepository
from app.shared.enums.order_status import OrderStatus
from app.shared.enums.product_status import ProductStatus
from app.shared.enums.roles import UserRole

SMALL, LARGE = 2, 25


# --- measuring -------------------------------------------------------------------------


@pytest_asyncio.fixture
async def statements(test_engine):
    captured: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        captured.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", record)
    yield captured
    event.remove(test_engine.sync_engine, "before_cursor_execute", record)


async def count_statements(statements, send) -> int:
    """Runs `send()` (an awaitable returning an HTTP response) and counts its SQL."""
    statements.clear()
    response = await send()
    assert response.status_code < 400, response.text
    if os.environ.get("SHOW_SQL"):
        for sql in statements:
            print("   SQL>", " ".join(sql.split())[:110])
    return len(statements)


def assert_constant(small: int, large: int, *, ceiling: int) -> None:
    print(f"  {small} statements for {SMALL} rows, {large} for {LARGE}")
    assert small == large, (
        f"{large - small} more SQL statements for {LARGE} rows than for {SMALL}: "
        f"{small} vs {large} (an N+1 -- something loads related rows one at a time)"
    )
    assert large <= ceiling, f"{large} statements, expected at most {ceiling}"


# --- data ---------------------------------------------------------------------------------


def headers_for(user: User) -> dict:
    return {"Authorization": f"Bearer {create_access_token(str(user.id))}"}


async def make_user(db_session, role=UserRole.USER) -> User:
    unique = uuid.uuid4().hex[:8]
    user = User(
        id=uuid.uuid4(), username=f"u_{unique}", email=f"u_{unique}@example.com",
        password_hash="irrelevant", role=role, is_active=True,
    )  # fmt: skip
    db_session.add(user)
    await db_session.flush()
    return user


async def make_products(db_session, count: int, *, stock: int = 100) -> list[Product]:
    unique = uuid.uuid4().hex[:8]
    category = Category(name=f"C {unique}", slug=f"c-{unique}")
    db_session.add(category)
    await db_session.flush()
    products = [
        Product(
            name=f"P {unique} {i}", slug=f"p-{unique}-{i}", price_cents=1000 + i,
            sku=f"SKU-{unique}-{i}", stock=stock, category_id=category.id,
            status=ProductStatus.ACTIVE,
        )  # fmt: skip
        for i in range(count)
    ]
    db_session.add_all(products)
    await db_session.flush()
    return products


async def make_cart(db_session, user: User, products: list[Product]) -> None:
    cart = Cart(user_id=user.id, items=[])
    db_session.add(cart)
    await db_session.flush()
    db_session.add_all(
        CartItem(
            cart_id=cart.id, product_id=p.id, quantity=1, price_cents_snapshot=p.price_cents
        )
        for p in products
    )
    await db_session.flush()


async def make_orders(db_session, user: User, products: list[Product], count: int) -> None:
    for _ in range(count):
        order = Order(
            id=uuid.uuid4(), user_id=user.id, idempotency_key=f"k-{uuid.uuid4().hex}",
            status=OrderStatus.PENDING, subtotal_cents=3000, shipping_cents=0,
            tax_cents=0, total_cents=3000,
        )  # fmt: skip
        db_session.add(order)
        await db_session.flush()
        for product in products[:3]:  # three lines per order
            db_session.add(
                OrderItem(
                    order_id=order.id, product_id=product.id,
                    product_name_snapshot=product.name,
                    price_cents_snapshot=product.price_cents, quantity=1,
                )
            )  # fmt: skip
    await db_session.flush()


# --- reads: list endpoints -------------------------------------------------------------------


async def test_product_list(client, db_session, statements):
    await make_products(db_session, LARGE)
    await db_session.commit()

    small = await count_statements(statements, lambda: client.get(f"/api/v1/products?page_size={SMALL}"))
    large = await count_statements(statements, lambda: client.get(f"/api/v1/products?page_size={LARGE}"))

    assert_constant(small, large, ceiling=3)  # count + page + categories


async def test_product_list_as_admin(client, db_session, statements):
    admin = await make_user(db_session, UserRole.ADMIN)
    await make_products(db_session, LARGE)
    await db_session.commit()
    h = headers_for(admin)

    small = await count_statements(statements, lambda: client.get(f"/api/v1/products?page_size={SMALL}", headers=h))
    large = await count_statements(statements, lambda: client.get(f"/api/v1/products?page_size={LARGE}", headers=h))

    assert_constant(small, large, ceiling=4)


async def test_category_list(client, db_session, statements):
    for i in range(LARGE):
        db_session.add(Category(name=f"Cat {i}", slug=f"cat-{i}"))
    await db_session.commit()

    small = await count_statements(statements, lambda: client.get(f"/api/v1/categories?page_size={SMALL}"))
    large = await count_statements(statements, lambda: client.get(f"/api/v1/categories?page_size={LARGE}"))

    assert_constant(small, large, ceiling=2)


async def test_my_orders_with_their_items(client, db_session, statements):
    user = await make_user(db_session)
    products = await make_products(db_session, 3)
    await make_orders(db_session, user, products, LARGE)
    await db_session.commit()
    h = headers_for(user)

    small = await count_statements(statements, lambda: client.get(f"/api/v1/orders?page_size={SMALL}", headers=h))
    large = await count_statements(statements, lambda: client.get(f"/api/v1/orders?page_size={LARGE}", headers=h))

    assert_constant(small, large, ceiling=4)  # user, count, orders, items


async def test_admin_all_orders_with_their_items(client, db_session, statements):
    admin = await make_user(db_session, UserRole.ADMIN)
    user = await make_user(db_session)
    products = await make_products(db_session, 3)
    await make_orders(db_session, user, products, LARGE)
    await db_session.commit()
    h = headers_for(admin)

    small = await count_statements(statements, lambda: client.get(f"/api/v1/orders/admin?page_size={SMALL}", headers=h))
    large = await count_statements(statements, lambda: client.get(f"/api/v1/orders/admin?page_size={LARGE}", headers=h))

    assert_constant(small, large, ceiling=4)


async def test_cart_with_products(client, db_session, statements):
    small_user, large_user = await make_user(db_session), await make_user(db_session)
    products = await make_products(db_session, LARGE)
    await make_cart(db_session, small_user, products[:SMALL])
    await make_cart(db_session, large_user, products)
    await db_session.commit()

    small = await count_statements(statements, lambda: client.get("/api/v1/cart", headers=headers_for(small_user)))
    large = await count_statements(statements, lambda: client.get("/api/v1/cart", headers=headers_for(large_user)))

    assert_constant(small, large, ceiling=5)  # user, cart, items, products


async def test_audit_log_list(client, db_session, statements):
    admin = await make_user(db_session, UserRole.ADMIN)
    for i in range(LARGE):
        db_session.add(AuditLog(actor_user_id=admin.id, action="x.created", target_type="x", target_id=str(i)))
    await db_session.commit()
    h = headers_for(admin)

    small = await count_statements(statements, lambda: client.get(f"/api/v1/audit-logs?page_size={SMALL}", headers=h))
    large = await count_statements(statements, lambda: client.get(f"/api/v1/audit-logs?page_size={LARGE}", headers=h))

    assert_constant(small, large, ceiling=3)


async def test_address_list(client, db_session, statements):
    small_user, large_user = await make_user(db_session), await make_user(db_session)
    for user, count in ((small_user, SMALL), (large_user, LARGE)):
        for i in range(count):
            db_session.add(
                Address(
                    user_id=user.id, recipient_name="R", line1=f"{i} Street", city="C",
                    state="S", postal_code="1", country="US",
                )  # fmt: skip
            )
    await db_session.commit()

    small = await count_statements(statements, lambda: client.get("/api/v1/addresses", headers=headers_for(small_user)))
    large = await count_statements(statements, lambda: client.get("/api/v1/addresses", headers=headers_for(large_user)))

    assert_constant(small, large, ceiling=2)


async def test_user_list(client, db_session, statements):
    admin = await make_user(db_session, UserRole.ADMIN)
    for _ in range(LARGE):
        await make_user(db_session)
    await db_session.commit()
    h = headers_for(admin)

    small = await count_statements(statements, lambda: client.get(f"/api/v1/users?page_size={SMALL}", headers=h))
    large = await count_statements(statements, lambda: client.get(f"/api/v1/users?page_size={LARGE}", headers=h))

    assert_constant(small, large, ceiling=3)


# --- GraphQL ---------------------------------------------------------------------------------------

USERS_QUERY = """
query Users($pageSize: Int!) {
  users(page: 1, pageSize: $pageSize) { total items { id username email role isActive } }
}
"""


async def test_graphql_users_list(client, db_session, statements):
    """
    The GraphQL schema exposes users only (no nested relationships), so this
    is a guard: the day a field such as `orders` is added to UserType, a
    resolver that loads it per user makes this test fail.
    """
    admin = await make_user(db_session, UserRole.ADMIN)
    for _ in range(LARGE):
        await make_user(db_session)
    await db_session.commit()
    h = headers_for(admin)

    async def query(size):
        return await client.post(
            "/graphql", json={"query": USERS_QUERY, "variables": {"pageSize": size}}, headers=h
        )

    def ok(response):
        assert "errors" not in response.json(), response.json()
        return response

    async def run(size):
        statements.clear()
        ok(await query(size))
        return len(statements)

    small, large = await run(SMALL), await run(LARGE)

    assert_constant(small, large, ceiling=3)  # user lookup, count, page


# --- writes: checkout ---------------------------------------------------------------------------------


async def _checkout(client, user, key):
    return await client.post(
        "/api/v1/orders/checkout",
        headers={**headers_for(user), "Idempotency-Key": key},
        json={
            "shipping_address": {
                "recipient_name": "R", "line1": "1 Street", "city": "C",
                "state": "S", "postal_code": "1", "country": "US",
            }  # fmt: skip
        },
    )


async def test_checkout_does_not_query_per_cart_line(client, db_session, statements):
    small_user, large_user = await make_user(db_session), await make_user(db_session)
    products = await make_products(db_session, LARGE)
    await make_cart(db_session, small_user, products[:SMALL])
    await make_cart(db_session, large_user, products)
    await db_session.commit()

    small = await count_statements(statements, lambda: _checkout(client, small_user, "k-small"))
    large = await count_statements(statements, lambda: _checkout(client, large_user, "k-large"))

    assert_constant(small, large, ceiling=20)


async def test_checkout_decrements_every_products_stock_exactly(client, db_session):
    user = await make_user(db_session)
    products = await make_products(db_session, 5, stock=10)
    await make_cart(db_session, user, products)
    # Different quantities per line, so a mix-up between lines would show.
    for quantity, item in enumerate(
        (await db_session.execute(select(CartItem).order_by(CartItem.product_id))).scalars(), 1
    ):
        item.quantity = quantity
    await db_session.commit()

    response = await _checkout(client, user, "k-math")
    assert response.status_code == 201

    stocks = (
        await db_session.execute(
            select(Product.stock).where(Product.id.in_([p.id for p in products])).order_by(Product.id)
        )
    ).scalars().all()
    assert stocks == [10 - 1, 10 - 2, 10 - 3, 10 - 4, 10 - 5]


async def test_checkout_that_runs_out_of_stock_on_one_line_changes_nothing(client, db_session):
    user = await make_user(db_session)
    products = await make_products(db_session, 3, stock=10)
    ids = [p.id for p in products]
    await make_cart(db_session, user, products)
    await db_session.execute(update(Product).where(Product.id == ids[2]).values(stock=0))
    await db_session.commit()

    response = await _checkout(client, user, "k-short")
    assert response.status_code >= 400

    db_session.expire_all()
    stocks = (
        await db_session.execute(select(Product.stock).where(Product.id.in_(ids)).order_by(Product.id))
    ).scalars().all()
    assert stocks == [10, 10, 0]  # the lines before the short one were not sold either


# --- the stale-order job: per-order cost must not depend on how many lines an order has -----------------


async def _stale_orders(db_session, *, orders: int, lines: int) -> None:
    user = await make_user(db_session)
    products = await make_products(db_session, lines)
    for _ in range(orders):
        order = Order(
            id=uuid.uuid4(), user_id=user.id, idempotency_key=f"k-{uuid.uuid4().hex}",
            status=OrderStatus.PENDING, subtotal_cents=1, shipping_cents=0,
            tax_cents=0, total_cents=1,
        )  # fmt: skip
        db_session.add(order)
        await db_session.flush()
        for product in products:
            db_session.add(
                OrderItem(
                    order_id=order.id, product_id=product.id, product_name_snapshot="x",
                    price_cents_snapshot=1, quantity=1,
                )
            )  # fmt: skip
    await db_session.commit()
    await db_session.execute(
        update(Order).values(created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2))
    )
    await db_session.commit()


async def _cancel_stale(test_session_factory) -> list:
    async with test_session_factory() as session:
        service = OrderService(
            OrderRepository(session), CartRepository(session), ProductRepository(session)
        )
        cancelled = await service.cancel_stale_pending_orders(
            older_than=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=30)
        )
        await session.commit()
    return cancelled


async def test_stale_order_cancellation_cost_per_order_does_not_depend_on_its_lines(
    db_session, test_session_factory, statements
):
    """
    The job handles one order per transaction on purpose (so one slow or
    contested order never blocks the rest), so its total grows with the number
    of ORDERS. What must not grow is the cost of an order with more lines.
    """
    await _stale_orders(db_session, orders=3, lines=1)
    statements.clear()
    assert len(await _cancel_stale(test_session_factory)) == 3
    one_line = len(statements)

    await db_session.execute(delete(Order))
    await db_session.commit()

    await _stale_orders(db_session, orders=3, lines=SMALL * 5)  # ten lines per order
    statements.clear()
    assert len(await _cancel_stale(test_session_factory)) == 3
    ten_lines = len(statements)

    print(f"  {one_line} statements for 3 one-line orders, {ten_lines} for 3 ten-line orders")
    assert one_line == ten_lines


async def test_bulk_stock_update_leaves_loaded_products_current_and_clean(
    db_session, test_session_factory
):
    products = await make_products(db_session, 3, stock=10)
    ids = [p.id for p in products]
    await db_session.commit()

    async with test_session_factory() as session:
        repo = ProductRepository(session)
        locked = await repo.get_many_for_update(ids)
        await repo.adjust_stock_many(locked, {ids[0]: -4, ids[1]: +5, ids[2]: 0})

        assert [locked[i].stock for i in ids] == [6, 15, 10]  # objects reflect the new stock
        assert not session.dirty  # ... without being marked modified (no second UPDATE)
        await session.commit()

    db_session.expire_all()
    stocks = (
        await db_session.execute(select(Product.stock).where(Product.id.in_(ids)).order_by(Product.id))
    ).scalars().all()
    assert stocks == [6, 15, 10]


# --- login: merging a guest cart into the account's cart ------------------------------------------------


async def _login_with_guest_cart(client, user, token):
    from app.core.rate_limit import limiter

    limiter.reset()
    client.cookies.set("guest_cart_token", token)
    try:
        return await client.post(
            "/api/v1/auth/login", json={"email": user.email, "password": "Password123!"}
        )
    finally:
        client.cookies.clear()


async def test_login_merge_of_a_guest_cart(client, db_session, statements):
    from app.core.security import hash_password

    products = await make_products(db_session, LARGE)
    results = {}
    for label, lines in (("small", SMALL), ("large", LARGE)):
        user = await make_user(db_session)
        user.password_hash = hash_password("Password123!")
        await make_cart(db_session, user, products[:1])  # the account already has a cart
        guest_token = f"guest-{label}"
        guest_cart = Cart(guest_token=guest_token, items=[])
        db_session.add(guest_cart)
        await db_session.flush()
        # `lines` items, the first of which is also in the account cart (a quantity merge).
        db_session.add_all(
            CartItem(cart_id=guest_cart.id, product_id=p.id, quantity=1, price_cents_snapshot=p.price_cents)
            for p in products[:lines]
        )
        await db_session.commit()
        results[label] = await count_statements(
            statements, lambda u=user, t=guest_token: _login_with_guest_cart(client, u, t)
        )

    assert_constant(results["small"], results["large"], ceiling=20)


async def test_login_merge_gives_the_right_cart(client, db_session):
    """The faster merge must still produce the same cart."""
    from app.core.security import hash_password

    products = await make_products(db_session, 4)
    ids = [p.id for p in products]
    user = await make_user(db_session)
    user.password_hash = hash_password("Password123!")
    await make_cart(db_session, user, products[:2])  # account cart: p0 x1, p1 x1
    guest_cart = Cart(guest_token="guest-merge", items=[])
    db_session.add(guest_cart)
    await db_session.flush()
    db_session.add_all(  # guest cart: p1 x3 (overlaps), p2 x2, p3 x1 (new)
        CartItem(cart_id=guest_cart.id, product_id=pid, quantity=q, price_cents_snapshot=1000)
        for pid, q in ((ids[1], 3), (ids[2], 2), (ids[3], 1))
    )
    await db_session.commit()
    user_id = user.id

    response = await _login_with_guest_cart(client, user, "guest-merge")
    assert response.status_code == 200

    db_session.expire_all()
    cart = (await db_session.execute(select(Cart).where(Cart.user_id == user_id))).scalar_one()
    lines = (
        await db_session.execute(select(CartItem.product_id, CartItem.quantity).where(CartItem.cart_id == cart.id))
    ).all()
    assert dict(lines) == {ids[0]: 1, ids[1]: 4, ids[2]: 2, ids[3]: 1}
    assert (
        await db_session.execute(select(Cart).where(Cart.guest_token == "guest-merge"))
    ).scalar_one_or_none() is None
