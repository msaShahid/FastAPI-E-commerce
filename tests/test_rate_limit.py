import pytest
from httpx import AsyncClient

from app.core.rate_limit import limiter


@pytest.fixture(autouse=True)
def _reset_rate_limiter():

    limiter.reset()
    yield


async def test_register_rate_limit(client: AsyncClient):

    for i in range(5):
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "username": f"ratelimit-test-{i}",
                "email": f"ratelimit-{i}@example.com",
                "password": "StrongPassword123!",
            },
        )
        assert response.status_code == 201

    response = await client.post(
        "/api/v1/auth/register",
        json={
            "username": "ratelimit-test-6",
            "email": "ratelimit-6@example.com",
            "password": "StrongPassword123!",
        },
    )
    assert response.status_code == 429


async def test_login_rate_limit(client: AsyncClient):

    payload = {"email": "nonexistent@example.com", "password": "wrong-password"}

    for _ in range(10):
        response = await client.post("/api/v1/auth/login", json=payload)
        assert response.status_code == 401 

    response = await client.post("/api/v1/auth/login", json=payload)
    assert response.status_code == 429


async def test_refresh_rate_limit(client: AsyncClient):

    payload = {"refresh_token": "invalid-refresh-token"}

    for _ in range(20):
        response = await client.post("/api/v1/auth/refresh", json=payload)
        assert response.status_code == 401 

    response = await client.post("/api/v1/auth/refresh", json=payload)
    assert response.status_code == 429