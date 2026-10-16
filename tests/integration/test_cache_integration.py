"""
The Redis response cache, against a real Redis and a real Postgres.

Two things matter and both are tested here:
  * it actually saves work (a cache HIT runs no SQL at all), and
  * it never serves stale data (every write path invalidates, a request that
    reads while a write commits cannot re-poison the cache, Redis being down
    degrades to "no cache", not to errors).

The root conftest switches the cache OFF for every test; the `cache` fixture
below switches it ON with a private key prefix so tests cannot see each
other's entries.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import event, update

from app.core.cache import (
    BYPASS,
    CATEGORIES,
    HIT,
    MISS,
    PRODUCTS,
    Cache,
    flush_pending,
    set_cache,
)
from app.core.config import get_settings
from app.modules.auth.models.user import User
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

PRODUCTS_URL = "/api/v1/products"
CATEGORIES_URL = "/api/v1/categories"


# --- fixtures -------------------------------------------------------------------


@pytest_asyncio.fixture
async def redis_client():
    client = Redis.from_url(get_settings().redis_url, decode_responses=True)
    yield client
    await client.aclose()


@pytest_asyncio.fixture
async def cache(redis_client):
    prefix = f"test-{uuid.uuid4().hex[:8]}"
    cache = Cache(redis_client, prefix=prefix, failure_cooldown=0.0)
    set_cache(cache)
    yield cache
    keys = [key async for key in redis_client.scan_iter(f"{prefix}:*")]
    if keys:
        await redis_client.delete(*keys)


@pytest_asyncio.fixture
async def query_counter(test_engine):
    """Counts SQL statements the application sends to Postgres."""
    statements: list[str] = []

    def count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", count)
    yield statements
    event.remove(test_engine.sync_engine, "before_cursor_execute", count)


async def _make_category(db_session, name="Gadgets") -> Category:
    category = Category(name=name, slug=name.lower().replace(" ", "-"))
    db_session.add(category)
    await db_session.commit()
    await flush_pending(db_session)  # what get_db does after every commit
    return category


async def _make_product(db_session, category, *, sku="SKU-1", name="Widget", **overrides) -> Product:
    fields = {
        "name": name,
        "slug": name.lower().replace(" ", "-"),
        "price_cents": 1000,
        "sku": sku,
        "stock": 5,
        "category_id": category.id,
        "status": ProductStatus.ACTIVE,
    }
    fields.update(overrides)
    product = Product(**fields)
    db_session.add(product)
    await db_session.commit()
    await flush_pending(db_session)
    return product


def _names(response) -> list[str]:
    return [item["name"] for item in response.json()["items"]]


# --- hits and misses ---------------------------------------------------------------


async def test_second_product_list_is_a_hit_with_identical_body(client, db_session, cache):
    category = await _make_category(db_session)
    await _make_product(db_session, category)

    first = await client.get(PRODUCTS_URL)
    second = await client.get(PRODUCTS_URL)

    assert first.headers["X-Cache"] == MISS
    assert second.headers["X-Cache"] == HIT
    assert second.content == first.content
    assert first.json()["total"] == 1


async def test_cached_body_matches_the_uncached_body(client, db_session, cache):
    """What the cache returns must be byte-for-byte what the database path returns."""
    category = await _make_category(db_session)
    await _make_product(db_session, category)

    cached = await client.get(PRODUCTS_URL)  # MISS, goes through the cache

    set_cache(Cache.disabled())
    uncached = await client.get(PRODUCTS_URL)

    assert "X-Cache" not in uncached.headers
    assert cached.json() == uncached.json()


async def test_a_hit_runs_no_sql(client, db_session, cache, query_counter):
    category = await _make_category(db_session)
    await _make_product(db_session, category)
    await client.get(PRODUCTS_URL)  # fills the cache
    query_counter.clear()

    response = await client.get(PRODUCTS_URL)

    assert response.headers["X-Cache"] == HIT
    assert query_counter == []


async def test_category_list_is_cached(client, db_session, cache, query_counter):
    await _make_category(db_session)
    await client.get(CATEGORIES_URL)
    query_counter.clear()

    response = await client.get(CATEGORIES_URL)

    assert response.headers["X-Cache"] == HIT
    assert query_counter == []


async def test_different_query_params_are_cached_separately(client, db_session, cache):
    category = await _make_category(db_session)
    await _make_product(db_session, category, sku="A", name="Alpha", price_cents=500)
    await _make_product(db_session, category, sku="B", name="Beta", price_cents=5000)

    cheap = await client.get(PRODUCTS_URL, params={"max_price": 1000})
    pricey = await client.get(PRODUCTS_URL, params={"min_price": 1000})
    cheap_again = await client.get(PRODUCTS_URL, params={"max_price": 1000})

    assert _names(cheap) == ["Alpha"]
    assert _names(pricey) == ["Beta"]
    assert cheap_again.headers["X-Cache"] == HIT
    assert _names(cheap_again) == ["Alpha"]


async def test_search_is_never_cached(client, db_session, cache):
    category = await _make_category(db_session)
    await _make_product(db_session, category, name="Searchable")

    first = await client.get(PRODUCTS_URL, params={"search": "search"})
    second = await client.get(PRODUCTS_URL, params={"search": "search"})

    assert first.headers["X-Cache"] == BYPASS
    assert second.headers["X-Cache"] == BYPASS
    assert _names(second) == ["Searchable"]


async def test_admin_sees_drafts_and_never_fills_the_public_cache(
    client, db_session, cache, admin_headers
):
    category = await _make_category(db_session)
    await _make_product(db_session, category, sku="LIVE", name="Live Product")
    await _make_product(
        db_session, category, sku="DRAFT", name="Draft Product", status=ProductStatus.DRAFT
    )

    admin_view = await client.get(PRODUCTS_URL, headers=admin_headers)
    public_view = await client.get(PRODUCTS_URL)
    public_again = await client.get(PRODUCTS_URL)

    assert admin_view.headers["X-Cache"] == BYPASS
    assert sorted(_names(admin_view)) == ["Draft Product", "Live Product"]
    assert _names(public_view) == ["Live Product"]
    assert public_again.headers["X-Cache"] == HIT
    assert _names(public_again) == ["Live Product"]


# --- invalidation: every write path ---------------------------------------------------


async def test_creating_a_product_invalidates_the_list(client, db_session, cache, admin_headers):
    category = await _make_category(db_session)
    assert (await client.get(PRODUCTS_URL)).json()["total"] == 0
    assert (await client.get(PRODUCTS_URL)).headers["X-Cache"] == HIT

    created = await client.post(
        PRODUCTS_URL,
        headers=admin_headers,
        json={
            "name": "Fresh",
            "price_cents": 1500,
            "sku": "FRESH-1",
            "stock": 3,
            "category_id": category.id,
            "status": "active",
        },
    )
    assert created.status_code == 201

    after = await client.get(PRODUCTS_URL)
    assert after.headers["X-Cache"] == MISS
    assert _names(after) == ["Fresh"]


async def test_updating_a_product_invalidates_the_list(client, db_session, cache, admin_headers):
    category = await _make_category(db_session)
    product = await _make_product(db_session, category, name="Old Name")
    await client.get(PRODUCTS_URL)

    updated = await client.patch(
        f"{PRODUCTS_URL}/{product.id}", headers=admin_headers, json={"name": "New Name"}
    )
    assert updated.status_code == 200

    after = await client.get(PRODUCTS_URL)
    assert after.headers["X-Cache"] == MISS
    assert _names(after) == ["New Name"]


async def test_archiving_a_product_removes_it_from_the_cached_list(
    client, db_session, cache, admin_headers
):
    category = await _make_category(db_session)
    product = await _make_product(db_session, category)
    assert _names(await client.get(PRODUCTS_URL)) == ["Widget"]

    archived = await client.delete(f"{PRODUCTS_URL}/{product.id}", headers=admin_headers)
    assert archived.status_code in (200, 204)

    assert _names(await client.get(PRODUCTS_URL)) == []


async def test_a_stock_change_invalidates_the_list(client, db_session, test_session_factory, cache):
    """Checkout and the stale-order job change stock without any admin request."""
    category = await _make_category(db_session)
    product = await _make_product(db_session, category, stock=5)
    assert (await client.get(PRODUCTS_URL)).json()["items"][0]["stock"] == 5

    # The same shape as checkout: lock, decrement, commit -- then flush_pending
    # (which get_db does for every request).
    async with test_session_factory() as session:
        repo = ProductRepository(session)
        locked = await repo.get_by_id_for_update(product.id)
        await repo.decrement_stock(locked, 2)
        await session.commit()
        await flush_pending(session)

    after = await client.get(PRODUCTS_URL)
    assert after.headers["X-Cache"] == MISS
    assert after.json()["items"][0]["stock"] == 3


async def test_a_real_checkout_invalidates_the_list(client, db_session, cache):
    """Checkout updates stock with one bulk UPDATE, which never goes through flush."""
    from app.core.security import create_access_token
    from app.modules.cart.models.cart import Cart
    from app.modules.cart.models.cart_item import CartItem

    category = await _make_category(db_session)
    product = await _make_product(db_session, category, stock=5)
    user = User(
        id=uuid.uuid4(), username="shopper", email="shopper@example.com",
        password_hash="irrelevant", role=UserRole.USER, is_active=True,
    )  # fmt: skip
    db_session.add(user)
    await db_session.flush()
    cart = Cart(user_id=user.id, items=[])
    db_session.add(cart)
    await db_session.flush()
    db_session.add(
        CartItem(cart_id=cart.id, product_id=product.id, quantity=2, price_cents_snapshot=1000)
    )
    await db_session.commit()
    assert (await client.get(PRODUCTS_URL)).json()["items"][0]["stock"] == 5
    assert (await client.get(PRODUCTS_URL)).headers["X-Cache"] == HIT

    response = await client.post(
        "/api/v1/orders/checkout",
        headers={
            "Authorization": f"Bearer {create_access_token(str(user.id))}",
            "Idempotency-Key": "k-1",
        },
        json={
            "shipping_address": {
                "recipient_name": "R", "line1": "1 Street", "city": "C",
                "state": "S", "postal_code": "1", "country": "US",
            }  # fmt: skip
        },
    )
    assert response.status_code == 201

    after = await client.get(PRODUCTS_URL)
    assert after.headers["X-Cache"] == MISS
    assert after.json()["items"][0]["stock"] == 3


async def test_stale_order_cancellation_invalidates_the_list(
    client, db_session, test_session_factory, cache
):
    category = await _make_category(db_session)
    product = await _make_product(db_session, category, stock=3)
    user = User(
        id=uuid.uuid4(),
        username="abandoner",
        email="abandoner@example.com",
        password_hash="irrelevant",
        role=UserRole.USER,
        is_active=True,
    )
    db_session.add(user)
    await db_session.flush()
    order = Order(
        id=uuid.uuid4(),
        user_id=user.id,
        idempotency_key=f"stale-{uuid.uuid4()}",
        status=OrderStatus.PENDING,
        subtotal_cents=2000,
        shipping_cents=0,
        tax_cents=0,
        total_cents=2000,
    )
    db_session.add(order)
    await db_session.flush()
    db_session.add(
        OrderItem(
            order_id=order.id,
            product_id=product.id,
            product_name_snapshot=product.name,
            price_cents_snapshot=product.price_cents,
            quantity=2,
        )
    )
    await db_session.commit()
    await db_session.execute(
        update(Order)
        .where(Order.id == order.id)
        .values(created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2))
    )
    await db_session.commit()

    assert (await client.get(PRODUCTS_URL)).json()["items"][0]["stock"] == 3

    # Exactly what scripts/cancel_stale_orders.py does.
    async with test_session_factory() as session:
        service = OrderService(
            OrderRepository(session), CartRepository(session), ProductRepository(session)
        )
        cancelled = await service.cancel_stale_pending_orders(
            older_than=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=30)
        )
        await session.commit()
        await flush_pending(session)

    assert cancelled == [order.id]
    assert (await client.get(PRODUCTS_URL)).json()["items"][0]["stock"] == 5


async def test_renaming_a_category_updates_cached_categories_and_cached_products(
    client, db_session, cache, admin_headers
):
    """A product list embeds its category, so both namespaces go stale."""
    category = await _make_category(db_session, name="Old Category")
    await _make_product(db_session, category)
    assert (await client.get(CATEGORIES_URL)).json()["items"][0]["name"] == "Old Category"
    products = await client.get(PRODUCTS_URL)
    assert products.json()["items"][0]["category"]["name"] == "Old Category"

    renamed = await client.patch(
        f"{CATEGORIES_URL}/{category.id}", headers=admin_headers, json={"name": "New Category"}
    )
    assert renamed.status_code == 200

    categories = await client.get(CATEGORIES_URL)
    products = await client.get(PRODUCTS_URL)
    assert categories.headers["X-Cache"] == MISS
    assert products.headers["X-Cache"] == MISS
    assert categories.json()["items"][0]["name"] == "New Category"
    assert products.json()["items"][0]["category"]["name"] == "New Category"


async def test_creating_a_category_invalidates_the_category_list(
    client, cache, admin_headers
):
    assert (await client.get(CATEGORIES_URL)).json()["total"] == 0
    assert (await client.get(CATEGORIES_URL)).headers["X-Cache"] == HIT

    created = await client.post(CATEGORIES_URL, headers=admin_headers, json={"name": "Brand New"})
    assert created.status_code == 201

    after = await client.get(CATEGORIES_URL)
    assert after.headers["X-Cache"] == MISS
    assert after.json()["total"] == 1


async def test_an_unrelated_write_does_not_invalidate(client, db_session, cache):
    category = await _make_category(db_session)
    await _make_product(db_session, category)
    await client.get(PRODUCTS_URL)

    db_session.add(
        User(
            id=uuid.uuid4(),
            username="bystander",
            email="bystander@example.com",
            password_hash="irrelevant",
            role=UserRole.USER,
            is_active=True,
        )
    )
    await db_session.commit()
    await flush_pending(db_session)

    assert (await client.get(PRODUCTS_URL)).headers["X-Cache"] == HIT


async def test_the_real_get_db_invalidates_after_commit_and_not_after_rollback(
    db_session, test_session_factory, cache, redis_client, monkeypatch
):
    """
    Every other test goes through the conftest override of get_db. This one
    drives the REAL get_db (pointed at the test database), so deleting its
    flush_pending call would be caught.
    """
    import app.core.database as database

    monkeypatch.setattr(database, "async_session_factory", test_session_factory)
    category = await _make_category(db_session)
    await cache.get_or_set(PRODUCTS, {}, 60, _loader("x"))
    version_key = cache._version_key(PRODUCTS)
    before = int(await redis_client.get(version_key))

    def new_product(sku):
        return Product(
            name=sku, slug=sku.lower(), price_cents=1, sku=sku, stock=1,
            category_id=category.id, status=ProductStatus.ACTIVE,
        )  # fmt: skip

    # Request that commits.
    gen = database.get_db()
    session = await gen.__anext__()
    session.add(new_product("COMMITTED"))
    await session.flush()
    assert int(await redis_client.get(version_key)) == before  # not before the commit
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()
    assert int(await redis_client.get(version_key)) == before + 1

    # Request that fails after writing.
    gen = database.get_db()
    session = await gen.__anext__()
    session.add(new_product("FAILED"))
    await session.flush()
    with pytest.raises(RuntimeError):
        await gen.athrow(RuntimeError("endpoint blew up"))
    assert int(await redis_client.get(version_key)) == before + 1


async def test_a_rolled_back_write_does_not_invalidate(db_session, cache, redis_client):
    category = await _make_category(db_session)
    await _make_product(db_session, category)
    await cache.get_or_set(PRODUCTS, {}, 60, _loader("x"))
    version_before = await redis_client.get(cache._version_key(PRODUCTS))

    db_session.add(
        Product(
            name="Rolled Back",
            slug="rolled-back",
            price_cents=1,
            sku="RB",
            stock=1,
            category_id=category.id,
            status=ProductStatus.ACTIVE,
        )
    )
    await db_session.flush()
    await db_session.rollback()
    await flush_pending(db_session)

    assert await redis_client.get(cache._version_key(PRODUCTS)) == version_before


# --- correctness under concurrency ---------------------------------------------------------


def _loader(value: str):
    async def load() -> str:
        return value

    return load


async def test_a_read_that_overlaps_a_write_cannot_leave_stale_data_in_the_cache(cache):
    """
    Reader starts (reads the version), the database answer is "old", a writer
    commits and invalidates, THEN the reader finishes and stores "old". The
    next request must not be served "old".
    """
    reader_has_version = asyncio.Event()
    writer_done = asyncio.Event()

    async def slow_loader() -> str:
        reader_has_version.set()  # the version has already been read
        await writer_done.wait()
        return "old"

    reader = asyncio.create_task(cache.get_or_set(PRODUCTS, {}, 60, slow_loader))
    await reader_has_version.wait()
    await cache.invalidate(PRODUCTS)  # the writer's commit + invalidation
    writer_done.set()
    assert await reader == ("old", MISS)

    payload, status = await cache.get_or_set(PRODUCTS, {}, 60, _loader("new"))
    assert (payload, status) == ("new", MISS)


async def test_concurrent_identical_misses_run_one_query(cache):
    calls = 0

    async def loader() -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.1)
        return "payload"

    results = await asyncio.gather(
        *(cache.get_or_set(PRODUCTS, {"page": 1}, 60, loader) for _ in range(25))
    )

    assert calls == 1
    assert {payload for payload, _ in results} == {"payload"}


async def test_a_failing_loader_does_not_poison_waiting_requests(cache):
    attempts = 0

    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(0.05)
        if attempts == 1:
            raise RuntimeError("database hiccup")
        return "ok"

    results = await asyncio.gather(
        *(cache.get_or_set(PRODUCTS, {}, 60, flaky) for _ in range(3)),
        return_exceptions=True,
    )

    assert sum(isinstance(r, RuntimeError) for r in results) == 1  # only the leader
    assert [r for r in results if not isinstance(r, Exception)] == [("ok", MISS)] * 2


# --- Redis misbehaving -----------------------------------------------------------------------


async def test_entries_expire(cache, redis_client):
    await cache.get_or_set(CATEGORIES, {}, 1, _loader("x"))

    keys = [k async for k in redis_client.scan_iter(f"{cache._prefix}:cache:{CATEGORIES}:*")]
    assert len(keys) == 1
    assert 0 < await redis_client.ttl(keys[0]) <= 1


async def test_losing_the_version_key_never_revives_old_entries(cache, redis_client):
    """Redis eviction or a FLUSH of just that key must not make old data valid again."""
    await cache.get_or_set(PRODUCTS, {}, 60, _loader("old"))
    assert (await cache.get_or_set(PRODUCTS, {}, 60, _loader("new")))[1] == HIT

    await redis_client.delete(cache._version_key(PRODUCTS))

    payload, status = await cache.get_or_set(PRODUCTS, {}, 60, _loader("new"))
    assert (payload, status) == ("new", MISS)


async def test_version_key_loss_followed_by_a_write_never_revives_old_entries(
    cache, redis_client
):
    """INCR on a missing key would restart at 1; versions must never go back to small numbers."""
    await cache.get_or_set(PRODUCTS, {}, 60, _loader("a"))
    await cache.invalidate(PRODUCTS)
    await cache.get_or_set(PRODUCTS, {}, 60, _loader("b"))  # stored under the new version

    await redis_client.delete(cache._version_key(PRODUCTS))
    await cache.invalidate(PRODUCTS)  # the version key does not exist

    payload, status = await cache.get_or_set(PRODUCTS, {}, 60, _loader("c"))
    assert (payload, status) == ("c", MISS)


async def test_redis_down_requests_still_succeed(client, db_session):
    category = await _make_category(db_session)
    await _make_product(db_session, category)
    dead = Redis.from_url(
        "redis://127.0.0.1:1/0",  # nothing listens here
        decode_responses=True,
        socket_timeout=0.2,
        socket_connect_timeout=0.2,
    )
    set_cache(Cache(dead, prefix="down", failure_cooldown=30.0))

    first = await client.get(PRODUCTS_URL)
    second = await client.get(PRODUCTS_URL)

    assert first.status_code == second.status_code == 200
    assert first.headers["X-Cache"] == second.headers["X-Cache"] == BYPASS
    assert _names(second) == ["Widget"]
    await dead.aclose()


async def test_after_a_failure_redis_is_skipped_during_the_cooldown():
    dead = Redis.from_url(
        "redis://127.0.0.1:1/0", socket_timeout=0.2, socket_connect_timeout=0.2
    )
    cache = Cache(dead, prefix="down", failure_cooldown=30.0)

    await cache.get_or_set(PRODUCTS, {}, 60, _loader("x"))  # discovers Redis is down
    started = asyncio.get_running_loop().time()
    for _ in range(50):
        await cache.get_or_set(PRODUCTS, {}, 60, _loader("x"))
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.1  # 50 requests, none of them waited on Redis
    await dead.aclose()


async def test_an_invalidation_missed_while_redis_was_down_is_replayed(redis_client):
    prefix = f"test-{uuid.uuid4().hex[:8]}"
    cache = Cache(redis_client, prefix=prefix, failure_cooldown=0.0)
    try:
        await cache.get_or_set(PRODUCTS, {}, 60, _loader("old"))

        # Redis "drops" the invalidation: the INCR fails.
        real_incr = redis_client.incr

        async def failing_incr(*args, **kwargs):
            raise ConnectionError("simulated outage")

        redis_client.incr = failing_incr
        await cache.invalidate(PRODUCTS)
        redis_client.incr = real_incr

        # Redis is back. The first request replays the missed invalidation
        # instead of serving the entry that should be dead.
        payload, status = await cache.get_or_set(PRODUCTS, {}, 60, _loader("new"))
        assert (payload, status) == ("new", MISS)
    finally:
        keys = [key async for key in redis_client.scan_iter(f"{prefix}:*")]
        if keys:
            await redis_client.delete(*keys)


async def test_disabled_cache_adds_no_header_and_always_queries(client, db_session):
    category = await _make_category(db_session)
    await _make_product(db_session, category)

    first = await client.get(PRODUCTS_URL)
    await _make_product(db_session, category, sku="SKU-2", name="Second")
    second = await client.get(PRODUCTS_URL)

    assert "X-Cache" not in first.headers
    assert second.json()["total"] == 2
