"""Refresh-token replay detection must persist, and concurrent rotation must not fork.

These tests pin the STRICT rule (any replay of a rotated token is theft), so they run with the reuse grace window switched off
(REFRESH_REUSE_GRACE_SECONDS=0). The window itself, and what it still treats as theft, is covered by test_refresh_grace_integration.py."""

import asyncio

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.modules.auth.models.refresh_token import RefreshToken


@pytest.fixture
def strict(monkeypatch):
    monkeypatch.setattr(get_settings(), "refresh_reuse_grace_seconds", 0)

API = "/api/v1/auth"


async def _login(client):
    await client.post(
        f"{API}/register",
        json={"username": "rotator", "email": "rotator@example.com", "password": "UserPass123!"},
    )
    r = await client.post(f"{API}/login", json={"email": "rotator@example.com", "password": "UserPass123!"})
    return r.json()["refresh_token"]


async def _refresh(client, token):
    return await client.post(f"{API}/refresh", json={"refresh_token": token})


async def test_normal_rotation_chain_keeps_working(client):
    a = await _login(client)
    r1 = await _refresh(client, a)
    r2 = await _refresh(client, r1.json()["refresh_token"])
    assert r1.status_code == r2.status_code == 200


async def test_replaying_a_rotated_token_revokes_the_whole_family_persistently(client, db_session, strict):
    a = await _login(client)
    b = (await _refresh(client, a)).json()["refresh_token"]

    replay = await _refresh(client, a)  # the stolen/old token
    assert replay.status_code == 401

    # the legitimate newest token is now dead too: the revocation was committed, not rolled back
    assert (await _refresh(client, b)).status_code == 401
    rows = (await db_session.execute(select(RefreshToken))).scalars().all()
    assert rows and all(row.revoked for row in rows)


async def test_concurrent_refresh_with_one_token_succeeds_at_most_once(client, db_session, strict):
    token = await _login(client)
    results = await asyncio.gather(*[_refresh(client, token) for _ in range(4)])
    codes = sorted(r.status_code for r in results)
    assert codes.count(200) == 1, codes
    assert codes.count(401) == 3, codes
    # a replay was observed, so the family is revoked: no surviving token to fork from
    rows = (await db_session.execute(select(RefreshToken))).scalars().all()
    assert all(row.revoked for row in rows)
