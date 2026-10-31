"""
No endpoint may return an unbounded amount of data, or let a caller make the
database do an unbounded amount of work.

Three layers:
  * a generic check over the OpenAPI schema, so a NEW list endpoint that forgets
    its bounds fails here without anyone remembering to add a test for it;
  * behaviour probes against every list endpoint (what actually happens with an
    oversized or absurd page / page_size);
  * the collections that are not paginated (addresses, cart) are capped instead.
"""

import uuid

import pytest
from sqlalchemy import select

from app.core.security import create_access_token
from app.main import app
from app.modules.addresses.models.address import Address
from app.modules.audit.models.audit_log import AuditLog
from app.modules.auth.models.user import User
from app.modules.cart.models.cart import Cart
from app.modules.cart.models.cart_item import CartItem
from app.modules.categories.models.category import Category
from app.modules.products.models.product import Product
from app.shared.enums.product_status import ProductStatus
from app.shared.enums.roles import UserRole
from app.shared.limits import (
    MAX_ADDRESSES_PER_USER,
    MAX_CART_LINES,
    MAX_GRAPHQL_DEPTH,
    MAX_PAGE,
    MAX_PAGE_SIZE,
)

API = "/api/v1"

# Every endpoint that returns a page of rows, and who may call it.
PAGINATED = [
    ("/products", None),
    ("/categories", None),
    ("/orders", "user"),
    ("/orders/admin", "admin"),
    ("/users", "admin"),
    ("/audit-logs", "admin"),
]

# GET endpoints that return a list without page/page_size, and why that is bounded.
BOUNDED_UNPAGINATED = {
    f"{API}/addresses": "capped at MAX_ADDRESSES_PER_USER per user",
    f"{API}/admin/payment-providers/catalog": "one entry per supported provider, from a code registry (currently 2)",
    f"{API}/admin/email-providers/catalog": "one entry per supported email service, from a code registry (currently 3)",
    f"{API}/wishlist/ids": "only product ids, and a wishlist holds at most 200 products",
    f"{API}/store/states": "the fixed list of Indian states and union territories (37), from a code table",
    f"{API}/reviews/summaries": "at most 50 products per request (product_ids has max_length 50)",
    f"{API}/admin/payment-providers": "capped by the unique (provider, mode) constraint: at most 2 rows per supported provider",
}


# --- generic: the OpenAPI schema ----------------------------------------------------------------


def _get_operations():
    for path, operations in app.openapi()["paths"].items():
        operation = operations.get("get")
        if operation:
            yield path, operation


def _query_params(operation) -> dict:
    return {p["name"]: p["schema"] for p in operation.get("parameters", []) if p["in"] == "query"}


def _returns_a_list(operation) -> bool:
    ok = operation["responses"].get("200", {}).get("content", {}).get("application/json", {})
    schema = ok.get("schema", {})
    return "Paginated" in str(schema) or schema.get("type") == "array"


def test_every_page_size_parameter_has_an_upper_bound():
    checked = 0
    for path, operation in _get_operations():
        params = _query_params(operation)
        if "page_size" in params:
            checked += 1
            schema = params["page_size"]
            assert schema.get("maximum") is not None, f"GET {path}: page_size has no maximum"
            assert schema["maximum"] <= MAX_PAGE_SIZE, f"GET {path}: page_size allows {schema['maximum']}"
            assert schema.get("minimum", 0) >= 1, f"GET {path}: page_size can be 0 or negative"
    assert checked >= len(PAGINATED)  # the check really looked at the endpoints


def test_every_page_parameter_has_an_upper_bound():
    for path, operation in _get_operations():
        params = _query_params(operation)
        if "page" in params:
            schema = params["page"]
            assert schema.get("maximum") is not None, f"GET {path}: page has no maximum"
            assert schema["maximum"] <= MAX_PAGE, f"GET {path}: page allows {schema['maximum']}"
            assert schema.get("minimum", 0) >= 1, f"GET {path}: page can be 0 or negative"


def test_every_list_endpoint_is_paginated_or_explicitly_bounded():
    for path, operation in _get_operations():
        if "{" in path or not _returns_a_list(operation):
            continue
        if "page_size" in _query_params(operation):
            continue
        assert path in BOUNDED_UNPAGINATED, (
            f"GET {path} returns a list with no page_size. Paginate it, or cap the collection "
            f"and add it to BOUNDED_UNPAGINATED with the reason."
        )


def _max_length(schema: dict) -> int | None:
    """maxLength of a string schema, looking inside `str | None` (anyOf)."""
    if "maxLength" in schema:
        return schema["maxLength"]
    for option in schema.get("anyOf", []):
        if "maxLength" in option:
            return option["maxLength"]
    return None


def test_free_text_filters_have_a_length_limit():
    checked = 0
    for path, operation in _get_operations():
        for name, schema in _query_params(operation).items():
            if name in ("search", "action", "target_type"):
                checked += 1
                limit = _max_length(schema)
                assert limit, f"GET {path}: {name} has no maxLength"
                assert limit <= 255, f"GET {path}: {name} maxLength {limit} is not a real bound"
    # products.search and the audit log's two filters.
    assert checked >= 3


# --- data -----------------------------------------------------------------------------------------


async def _user(db_session, role=UserRole.USER) -> User:
    unique = uuid.uuid4().hex[:8]
    user = User(
        id=uuid.uuid4(), username=f"u_{unique}", email=f"u_{unique}@example.com",
        password_hash="irrelevant", role=role, is_active=True,
    )  # fmt: skip
    db_session.add(user)
    await db_session.flush()
    return user


def _headers(user: User) -> dict:
    return {"Authorization": f"Bearer {create_access_token(str(user.id))}"}


@pytest.fixture
async def callers(db_session):
    admin = await _user(db_session, UserRole.ADMIN)
    user = await _user(db_session)
    category = Category(name="Bounds", slug="bounds")
    db_session.add(category)
    await db_session.flush()
    db_session.add_all(
        Product(
            name=f"P{i}", slug=f"p{i}", price_cents=100, sku=f"S{i}", stock=1,
            category_id=category.id, status=ProductStatus.ACTIVE,
        )  # fmt: skip
        for i in range(30)
    )
    db_session.add_all(
        AuditLog(actor_user_id=admin.id, action="x.created", target_type="x", target_id=str(i))
        for i in range(30)
    )
    await db_session.commit()
    return {None: {}, "admin": _headers(admin), "user": _headers(user)}


# --- behaviour: every list endpoint ----------------------------------------------------------------------


@pytest.mark.parametrize(("path", "who"), PAGINATED)
@pytest.mark.parametrize("page_size", [MAX_PAGE_SIZE + 1, 1000, 0, -1, "abc", 10**30])
async def test_oversized_or_invalid_page_size_is_rejected(client, callers, path, who, page_size):
    response = await client.get(f"{API}{path}", params={"page_size": page_size}, headers=callers[who])
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(("path", "who"), PAGINATED)
@pytest.mark.parametrize("page", [0, -1, "abc", MAX_PAGE + 1, 10**18, 10**30])
async def test_absurd_page_is_rejected_not_a_server_error(client, callers, path, who, page):
    """A huge page used to become a huge OFFSET; past the int64 range that was an HTTP 500."""
    response = await client.get(f"{API}{path}", params={"page": page}, headers=callers[who])
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(("path", "who"), PAGINATED)
async def test_the_largest_allowed_page_and_page_size_work(client, callers, path, who):
    response = await client.get(
        f"{API}{path}",
        params={"page": MAX_PAGE, "page_size": MAX_PAGE_SIZE},
        headers=callers[who],
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(("path", "who"), [p for p in PAGINATED if p[0] in ("/products", "/audit-logs", "/users")])
async def test_a_page_never_has_more_rows_than_page_size(client, callers, path, who):
    response = await client.get(f"{API}{path}", params={"page_size": 5}, headers=callers[who])
    body = response.json()
    assert 0 < len(body["items"]) <= 5
    if path != "/users":  # the test database only has a couple of users
        assert len(body["items"]) == 5 and body["total"] >= 30


# --- GraphQL --------------------------------------------------------------------------------------------

USERS = """
query Users($page: Int!, $pageSize: Int!) {
  users(page: $page, pageSize: $pageSize) { total items { id } }
}
"""


async def _gql(client, headers, query, variables=None):
    return await client.post("/graphql", json={"query": query, "variables": variables or {}}, headers=headers)


@pytest.mark.parametrize(
    ("page", "page_size"),
    [(1, MAX_PAGE_SIZE + 1), (1, 0), (1, -1), (0, 10), (-1, 10), (MAX_PAGE + 1, 10), (2**31 - 1, 100)],
)
async def test_graphql_rejects_out_of_range_paging(client, callers, page, page_size):
    response = await _gql(client, callers["admin"], USERS, {"page": page, "pageSize": page_size})
    assert response.json().get("errors"), response.json()
    assert not (response.json().get("data") or {}).get("users")


async def test_graphql_allows_the_largest_page(client, callers):
    response = await _gql(client, callers["admin"], USERS, {"page": MAX_PAGE, "pageSize": MAX_PAGE_SIZE})
    assert "errors" not in response.json(), response.json()


async def test_graphql_aliases_cannot_multiply_the_page_size_cap(client, callers):
    """Ten aliased `users(pageSize: 100)` in one request would be 1000 rows despite the 100 cap."""
    many = "query {" + " ".join(f"a{i}: users(page: 1, pageSize: 100) {{ total }}" for i in range(50)) + "}"
    response = await _gql(client, callers["admin"], many)
    assert response.json().get("errors"), "50 aliases were accepted"


async def test_graphql_a_reasonable_query_with_a_few_aliases_still_works(client, callers):
    query = "query { a: users(page: 1, pageSize: 5) { total } b: users(page: 2, pageSize: 5) { total } me { id } }"
    response = await _gql(client, callers["admin"], query)
    assert "errors" not in response.json(), response.json()


def test_graphql_schema_limits_query_depth():
    """
    Today's schema has no self-referencing types, so no query can nest deeply and
    there is nothing to probe over HTTP. Guard the configuration instead, so a
    future recursive type (user -> orders -> user ...) is born with a depth limit.
    (Fragment spreads alone do not add depth; only nested fields do.)
    """
    from strawberry.extensions import QueryDepthLimiter

    from app.graphql.schema import schema

    limiters = [e for e in schema.extensions if isinstance(e, QueryDepthLimiter)]
    assert limiters, "GraphQL schema has no QueryDepthLimiter"
    assert MAX_GRAPHQL_DEPTH <= 10


# --- collections that are capped instead of paginated -----------------------------------------------------


def _address_payload(i: int) -> dict:
    return {
        "recipient_name": "R", "line1": f"{i} Street", "city": "C", "state": "S",
        "postal_code": "1", "country": "US",
    }  # fmt: skip


async def test_a_user_cannot_save_more_than_the_address_limit(client, db_session):
    user = await _user(db_session)
    await db_session.commit()
    headers = _headers(user)

    for i in range(MAX_ADDRESSES_PER_USER):
        response = await client.post(f"{API}/addresses", json=_address_payload(i), headers=headers)
        assert response.status_code == 201, response.text

    over = await client.post(f"{API}/addresses", json=_address_payload(999), headers=headers)
    assert over.status_code == 409
    assert str(MAX_ADDRESSES_PER_USER) in over.json()["detail"]
    listed = await client.get(f"{API}/addresses", headers=headers)
    assert len(listed.json()) == MAX_ADDRESSES_PER_USER

    # Deleting one makes room again.
    first_id = listed.json()[0]["id"]
    assert (await client.delete(f"{API}/addresses/{first_id}", headers=headers)).status_code == 204
    again = await client.post(f"{API}/addresses", json=_address_payload(1000), headers=headers)
    assert again.status_code == 201


async def test_the_address_limit_is_per_user(client, db_session):
    first, second = await _user(db_session), await _user(db_session)
    for i in range(MAX_ADDRESSES_PER_USER):
        db_session.add(
            Address(user_id=first.id, recipient_name="R", line1=f"{i}", city="C", state="S",
                    postal_code="1", country="US")
        )  # fmt: skip
    await db_session.commit()

    response = await client.post(f"{API}/addresses", json=_address_payload(1), headers=_headers(second))
    assert response.status_code == 201


async def _products(db_session, count: int) -> list[Product]:
    category = Category(name="Cart Cap", slug="cart-cap")
    db_session.add(category)
    await db_session.flush()
    products = [
        Product(
            name=f"C{i}", slug=f"c{i}", price_cents=100, sku=f"CC{i}", stock=5,
            category_id=category.id, status=ProductStatus.ACTIVE,
        )  # fmt: skip
        for i in range(count)
    ]
    db_session.add_all(products)
    await db_session.flush()
    return products


async def test_a_cart_cannot_hold_more_lines_than_the_limit(client, db_session):
    user = await _user(db_session)
    products = await _products(db_session, MAX_CART_LINES + 1)
    cart = Cart(user_id=user.id, items=[])
    db_session.add(cart)
    await db_session.flush()
    db_session.add_all(
        CartItem(cart_id=cart.id, product_id=p.id, quantity=1, price_cents_snapshot=100)
        for p in products[:MAX_CART_LINES]
    )
    await db_session.commit()
    headers = _headers(user)
    extra_id, existing_id = products[MAX_CART_LINES].id, products[0].id

    over = await client.post(f"{API}/cart/items", json={"product_id": extra_id, "quantity": 1}, headers=headers)
    assert over.status_code == 409
    assert str(MAX_CART_LINES) in over.json()["detail"]

    # More of something already in the cart is not a new line, so it is still allowed.
    more = await client.post(f"{API}/cart/items", json={"product_id": existing_id, "quantity": 1}, headers=headers)
    assert more.status_code == 201

    count = len((await client.get(f"{API}/cart", headers=headers)).json()["items"])
    assert count == MAX_CART_LINES


async def test_the_cart_limit_applies_to_guest_carts_too(client, db_session):
    products = await _products(db_session, MAX_CART_LINES + 1)
    await db_session.commit()
    ids = [p.id for p in products]

    for product_id in ids[:MAX_CART_LINES]:
        r = await client.post(f"{API}/cart/items", json={"product_id": product_id, "quantity": 1})
        assert r.status_code == 201, r.text

    over = await client.post(f"{API}/cart/items", json={"product_id": ids[MAX_CART_LINES], "quantity": 1})
    assert over.status_code == 409


async def test_nothing_was_added_by_the_rejected_requests(client, db_session):
    """The rejections above must not leave partial data behind."""
    user = await _user(db_session)
    await db_session.commit()
    headers = _headers(user)
    for i in range(MAX_ADDRESSES_PER_USER + 3):
        await client.post(f"{API}/addresses", json=_address_payload(i), headers=headers)

    rows = (await db_session.execute(select(Address).where(Address.user_id == user.id))).scalars().all()
    assert len(rows) == MAX_ADDRESSES_PER_USER
