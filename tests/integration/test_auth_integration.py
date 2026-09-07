
async def test_register_and_login_full_flow(client):
    """
    Register a user, then log in with the same credentials.
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
    assert "password_hash" not in body

    login_response = await client.post(
        "/api/v1/auth/login",
        json={
            "email": "integration@example.com",
            "password": "supersecret123",
        },
    )

    assert login_response.status_code == 200

    tokens = login_response.json()

    assert "access_token" in tokens
    assert "refresh_token" in tokens


async def test_duplicate_email_registration_returns_409_from_real_db(client):
    """
    The second registration with the same email must be rejected.
    """
    payload = {
        "username": "first",
        "email": "dup@example.com",
        "password": "supersecret123",
    }

    first = await client.post(
        "/api/v1/auth/register",
        json=payload,
    )

    assert first.status_code == 201

    payload["username"] = "second"

    second = await client.post(
        "/api/v1/auth/register",
        json=payload,
    )

    assert second.status_code == 409


async def test_protected_route_requires_real_valid_token(client):
    """
    A request without an Authorization token must be rejected.
    """
    response = await client.get("/api/v1/auth/me")

    assert response.status_code in (401, 403)


async def test_protected_route_works_with_real_token(client):
    """
    Register -> login -> use the real access token on /me.
    """
    register_response = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "meuser",
            "email": "meuser@example.com",
            "password": "supersecret123",
        },
    )

    assert register_response.status_code == 201

    login_response = await client.post(
        "/api/v1/auth/login",
        json={
            "email": "meuser@example.com",
            "password": "supersecret123",
        },
    )

    assert login_response.status_code == 200

    access_token = login_response.json()["access_token"]

    me_response = await client.get(
        "/api/v1/auth/me",
        headers={
            "Authorization": f"Bearer {access_token}",
        },
    )

    assert me_response.status_code == 200
    assert me_response.json()["email"] == "meuser@example.com"
