import pytest


@pytest.mark.asyncio
async def test_security_headers(client):
    response = await client.get("/")

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Strict-Transport-Security"] == (
        "max-age=63072000; includeSubDomains"
    )
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"