"""
End-to-end GraphQL tests: real HTTP POSTs to /graphql through the ASGI
app, real JWT authentication, real Postgres.

The existing user tests exercise UserService directly. These prove the
layer on top of it -- the GraphQL context (Bearer token parsing and
authentication), the resolvers, and how errors reach the client -- none
of which a service-level unit test can see.
"""

import pytest
from sqlalchemy import select

from app.modules.auth.models.user import User

GRAPHQL_URL = "/graphql"

ME_QUERY = "query { me { id username email role isActive } }"

USERS_QUERY = """
query Users($page: Int!, $pageSize: Int!) {
  users(page: $page, pageSize: $pageSize) {
    total
    page
    pageSize
    totalPages
    items { id username role }
  }
}
"""

DEACTIVATE_MUTATION = "mutation Deactivate($id: UUID!) { deactivateUser(userId: $id) }"

UPDATE_MUTATION = """
mutation Update($input: UpdateUserInput!) {
  updateUser(input: $input) { id username role }
}
"""


async def gql(client, query, variables=None, headers=None):
    return await client.post(
        GRAPHQL_URL,
        json={"query": query, "variables": variables or {}},
        headers=headers or {},
    )


async def register_and_login(client, username):
    """Registers a regular user through the REST API and returns (user_id, auth headers)."""
    email = f"{username}@example.com"
    password = "supersecret123"
    register = await client.post(
        "/api/v1/auth/register",
        json={"username": username, "email": email, "password": password},
    )
    assert register.status_code == 201
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    token = login.json()["access_token"]
    return register.json()["id"], {"Authorization": f"Bearer {token}"}


async def admin_user_id(client, admin_headers):
    response = await client.get("/api/v1/users/me", headers=admin_headers)
    return response.json()["id"]


# --------------------------------------------------------------------------
# Authentication: every GraphQL operation needs a valid access token
# --------------------------------------------------------------------------


async def test_me_returns_the_authenticated_user(client):
    user_id, headers = await register_and_login(client, "gqlme")

    response = await gql(client, ME_QUERY, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert "errors" not in body
    assert body["data"]["me"] == {
        "id": user_id,
        "username": "gqlme",
        "email": "gqlme@example.com",
        "role": "user",
        "isActive": True,
    }


async def test_request_without_authorization_header_is_rejected(client):
    response = await gql(client, ME_QUERY)

    assert response.status_code == 401


async def test_request_with_garbage_token_is_rejected(client):
    response = await gql(client, ME_QUERY, headers={"Authorization": "Bearer not-a-real-token"})

    assert response.status_code == 401


async def test_request_with_non_bearer_scheme_is_rejected(client):
    response = await gql(client, ME_QUERY, headers={"Authorization": "Basic abc123"})

    assert response.status_code == 401


async def test_refresh_token_is_rejected_as_access_token(client):
    await client.post(
        "/api/v1/auth/register",
        json={"username": "gqlrefresh", "email": "gqlrefresh@example.com",
              "password": "supersecret123"},
    )
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "gqlrefresh@example.com", "password": "supersecret123"},
    )
    refresh_token = login.json()["refresh_token"]

    response = await gql(
        client, ME_QUERY, headers={"Authorization": f"Bearer {refresh_token}"}
    )

    assert response.status_code == 401


# --------------------------------------------------------------------------
# deactivateUser: the full mutation, including every way it can be refused
# --------------------------------------------------------------------------


async def test_admin_can_deactivate_another_user(client, admin_headers, db_session):
    target_id, _ = await register_and_login(client, "gqltarget")

    response = await gql(client, DEACTIVATE_MUTATION, {"id": target_id}, admin_headers)

    assert response.status_code == 200
    body = response.json()
    assert "errors" not in body
    assert body["data"]["deactivateUser"] is True

    # The change is really persisted, not just reported.
    db_session.expire_all()
    user = (await db_session.execute(select(User).where(User.id == target_id))).scalar_one()
    assert user.is_active is False


async def test_deactivated_user_can_no_longer_use_their_token(client, admin_headers):
    target_id, target_headers = await register_and_login(client, "gqllockedout")
    assert (await gql(client, ME_QUERY, headers=target_headers)).status_code == 200

    await gql(client, DEACTIVATE_MUTATION, {"id": target_id}, admin_headers)

    # Same token that worked a moment ago is now refused.
    response = await gql(client, ME_QUERY, headers=target_headers)
    assert response.status_code == 401


async def test_regular_user_cannot_deactivate_someone_else(client, db_session):
    _, attacker_headers = await register_and_login(client, "gqlattacker")
    victim_id, _ = await register_and_login(client, "gqlvictim")

    response = await gql(client, DEACTIVATE_MUTATION, {"id": victim_id}, attacker_headers)

    assert response.status_code == 200  # GraphQL reports resolver errors in the body
    body = response.json()
    assert body["data"] is None
    assert "only deactivate your own account" in body["errors"][0]["message"]

    db_session.expire_all()
    victim = (await db_session.execute(select(User).where(User.id == victim_id))).scalar_one()
    assert victim.is_active is True


async def test_unauthenticated_request_cannot_deactivate_anyone(client, db_session):
    victim_id, _ = await register_and_login(client, "gqlbystander")

    response = await gql(client, DEACTIVATE_MUTATION, {"id": victim_id})

    assert response.status_code == 401
    db_session.expire_all()
    victim = (await db_session.execute(select(User).where(User.id == victim_id))).scalar_one()
    assert victim.is_active is True


async def test_deactivating_a_nonexistent_user_is_an_error(client, admin_headers):
    missing_id = "00000000-0000-4000-8000-000000000000"

    response = await gql(client, DEACTIVATE_MUTATION, {"id": missing_id}, admin_headers)

    body = response.json()
    assert body["data"] is None
    assert body["errors"]


async def test_deactivate_with_malformed_uuid_is_rejected_by_the_schema(client, admin_headers):
    response = await gql(client, DEACTIVATE_MUTATION, {"id": "not-a-uuid"}, admin_headers)

    body = response.json()
    assert body["errors"]
    assert body.get("data") is None


# --------------------------------------------------------------------------
# updateUser: the privilege-escalation guard, through the GraphQL layer
# --------------------------------------------------------------------------


async def test_user_can_update_their_own_username(client):
    user_id, headers = await register_and_login(client, "gqlrename")

    response = await gql(
        client, UPDATE_MUTATION, {"input": {"userId": user_id, "username": "gqlrenamed"}}, headers
    )

    body = response.json()
    assert "errors" not in body
    assert body["data"]["updateUser"]["username"] == "gqlrenamed"


async def test_user_cannot_promote_themselves_to_admin(client, db_session):
    user_id, headers = await register_and_login(client, "gqlescalate")

    response = await gql(
        client, UPDATE_MUTATION, {"input": {"userId": user_id, "role": "ADMIN"}}, headers
    )

    body = response.json()
    assert body.get("data") is None
    assert body["errors"]

    db_session.expire_all()
    user = (await db_session.execute(select(User).where(User.id == user_id))).scalar_one()
    assert user.role.value == "user"


# --------------------------------------------------------------------------
# users query: admin-only, paginated, validated
# --------------------------------------------------------------------------


async def test_admin_can_list_users_with_pagination(client, admin_headers):
    for i in range(3):
        await register_and_login(client, f"gqllist{i}")

    response = await gql(client, USERS_QUERY, {"page": 1, "pageSize": 2}, admin_headers)

    body = response.json()
    assert "errors" not in body
    page = body["data"]["users"]
    assert page["total"] == 4  # 3 registered above + the admin fixture's user
    assert page["pageSize"] == 2
    assert page["totalPages"] == 2
    assert len(page["items"]) == 2


async def test_regular_user_cannot_list_users(client):
    _, headers = await register_and_login(client, "gqlnosy")

    response = await gql(client, USERS_QUERY, {"page": 1, "pageSize": 10}, headers)

    body = response.json()
    assert body.get("data") is None
    assert "Only administrators" in body["errors"][0]["message"]


@pytest.mark.parametrize("page,page_size", [(0, 10), (1, 0), (1, 101)])
async def test_users_query_rejects_invalid_pagination(client, admin_headers, page, page_size):
    response = await gql(client, USERS_QUERY, {"page": page, "pageSize": page_size}, admin_headers)

    body = response.json()
    assert body.get("data") is None
    assert body["errors"]
