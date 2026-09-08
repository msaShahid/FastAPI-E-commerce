"""
Integration test for the users/auth boundary -- specifically the one
behavior that only a real database can prove: deleting a user row
CASCADEs to their refresh_tokens (Stage 5's ondelete="CASCADE"
decision, deliberately different from products' RESTRICT).
"""

from sqlalchemy import select

from app.modules.auth.models.refresh_token import RefreshToken
from app.modules.auth.models.user import User


async def test_deleting_user_cascades_to_refresh_tokens(client, db_session):
    register_response = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "cascadetest",
            "email": "cascadetest@example.com",
            "password": "supersecret123",
        },
    )
    user_id = register_response.json()["id"]

    # Logging in creates a real refresh_tokens row for this user.
    await client.post(
        "/api/v1/auth/login",
        json={"email": "cascadetest@example.com", "password": "supersecret123"},
    )

    tokens_before = await db_session.execute(
        select(RefreshToken).where(RefreshToken.user_id == user_id)
    )
    assert len(tokens_before.scalars().all()) == 1

    user = await db_session.get(User, user_id)
    await db_session.delete(user)
    await db_session.flush()

    tokens_after = await db_session.execute(
        select(RefreshToken).where(RefreshToken.user_id == user_id)
    )
    assert tokens_after.scalars().all() == []