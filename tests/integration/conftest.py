import pytest_asyncio
from sqlalchemy import update

from app.modules.auth.models.user import User
from app.shared.enums.roles import UserRole

# This file only ADDS a new fixture -- it does NOT redefine `client` or
# `db_session`. Those are inherited automatically from the root
# tests/conftest.py. Redefining them here was the exact mistake that
# broke everything a few messages ago; this file is careful not to
# repeat it.


@pytest_asyncio.fixture
async def admin_headers(client, db_session):
    """
    Registers a real user through the real API, then promotes them to
    ADMIN directly via the database -- mirroring the same bootstrapping
    gap we identified back in Stage 6 (there's no API route that can
    create the first admin). get_current_user re-fetches the user's
    role from the database on every request, so this promotion takes
    effect immediately, on the SAME access token, without needing to
    log in again.
    """
    await client.post(
        "/api/v1/auth/register",
        json={
            "username": "admin_test",
            "email": "admin_test@example.com",
            "password": "AdminPassword123!",
        },
    )
    login_response = await client.post(
        "/api/v1/auth/login",
        json={"email": "admin_test@example.com", "password": "AdminPassword123!"},
    )
    access_token = login_response.json()["access_token"]

    await db_session.execute(
        update(User).where(User.email == "admin_test@example.com").values(role=UserRole.ADMIN)
    )
    await db_session.commit()

    return {"Authorization": f"Bearer {access_token}"}