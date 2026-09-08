"""
Integration tests for categories -- focused specifically on behavior a
fake repository CANNOT verify: real database constraints, and the full
real HTTP -> auth -> service -> DB chain end to end. Business logic
(slug generation, uniqueness pre-checks) is already thoroughly covered
by tests/categories/test_category_service.py against a fake repository
-- this file does not repeat that coverage.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from app.modules.categories.models.category import Category


async def test_category_name_unique_constraint_enforced_at_db_level(db_session):
    """
    Bypasses CategoryService entirely -- inserts two rows with the same
    name directly through the ORM. This proves the UNIQUE constraint
    from the Stage 4/7 migration is REALLY there, independent of
    CategoryService's own pre-check. Defense in depth, actually tested.
    """
    db_session.add(Category(name="Books", slug="books-1"))
    await db_session.flush()

    db_session.add(Category(name="Books", slug="books-2"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_category_slug_unique_constraint_enforced_at_db_level(db_session):
    db_session.add(Category(name="First Name", slug="same-slug"))
    await db_session.flush()

    db_session.add(Category(name="Second Name", slug="same-slug"))
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_create_category_without_admin_returns_403(client):
    """
    Proves Stage 5's AdminUser dependency actually enforces itself over
    a real HTTP request, not just in isolated dependency-function tests.
    """
    await client.post(
        "/api/v1/auth/register",
        json={
            "username": "regularuser",
            "email": "regular@example.com",
            "password": "supersecret123",
        },
    )
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "regular@example.com", "password": "supersecret123"},
    )
    token = login.json()["access_token"]

    response = await client.post(
        "/api/v1/categories",
        json={"name": "Electronics", "description": None},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


async def test_create_category_as_admin_succeeds_end_to_end(client, admin_headers):
    """
    The full real chain: real HTTP request -> real admin check -> real
    CategoryService -> real slug generation -> real INSERT -> real
    response shape.
    """
    response = await client.post(
        "/api/v1/categories",
        json={"name": "Home & Garden", "description": "Yard and household items."},
        headers=admin_headers,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["slug"] == "home-garden"
    assert body["is_active"] is True