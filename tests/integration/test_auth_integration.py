"""
Integration tests: real HTTP requests through the actual FastAPI app,
hitting a real (disposable) Postgres database -- see tests/conftest.py
for the fixtures making this possible.

"""


async def test_register_and_login_full_flow(client):
    """
    The most basic possible proof this whole setup works: register a
    user for real (real password hashing, real INSERT into a real
    users table), then log in with those exact credentials and get a
    real, valid JWT back.
    """
    register_response = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "integrationtest",
            "email": "integration@example.com",
            "password": "supersecret123",
        },
    )
    assert register_response.status_code == 201
    body = register_response.json()
    assert body["email"] == "integration@example.com"
    assert "password_hash" not in body  # UserRead correctly excludes it

    login_response = await client.post(
        "/api/v1/auth/login",
        json={"email": "integration@example.com", "password": "supersecret123"},
    )
    assert login_response.status_code == 200
    tokens = login_response.json()
    assert "access_token" in tokens
    assert "refresh_token" in tokens


async def test_duplicate_email_registration_returns_409_from_real_db(client):

    payload = {
        "username": "first",
        "email": "dup@example.com",
        "password": "supersecret123",
    }
    first = await client.post("/api/v1/auth/register", json=payload)
    assert first.status_code == 201

    payload["username"] = "second"
    second = await client.post("/api/v1/auth/register", json=payload)
    assert second.status_code == 409


async def test_protected_route_requires_real_valid_token(client):
    """
    Confirms get_current_user's full real chain: no token -> 401.
    """
    response = await client.get("/api/v1/auth/me")
    assert response.status_code in (401, 403)


async def test_protected_route_works_with_real_token(client):
    await client.post(
        "/api/v1/auth/register",
        json={
            "username": "meuser",
            "email": "meuser@example.com",
            "password": "supersecret123",
        },
    )
    login_response = await client.post(
        "/api/v1/auth/login",
        json={"email": "meuser@example.com", "password": "supersecret123"},
    )
    access_token = login_response.json()["access_token"]

    me_response = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert me_response.status_code == 200
    assert me_response.json()["email"] == "meuser@example.com"