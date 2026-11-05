"""Refresh-token reuse grace window over HTTP and the real database: two requests with the SAME refresh token at the same moment
(two tabs) must both succeed; reuse after the window is still theft; the replay is audited."""

import asyncio
import uuid

from sqlalchemy import text

from app.core.rate_limit import limiter

API = "/api/v1"


async def session_tokens(client):
    limiter.reset()
    name = f"u{uuid.uuid4().hex[:8]}"
    body = {"username": name, "email": f"{name}@example.com", "password": "CorrectHorse1!"}
    await client.post(f"{API}/auth/register", json=body)
    client.cookies.clear()
    return (await client.post(f"{API}/auth/login", json={"email": body["email"], "password": body["password"]})).json()


async def refresh(client, token):
    return await client.post(f"{API}/auth/refresh", json={"refresh_token": token})


async def test_two_tabs_refreshing_with_the_same_token_at_the_same_time_both_stay_signed_in(client):
    tokens = await session_tokens(client)
    a, b = await asyncio.gather(refresh(client, tokens["refresh_token"]), refresh(client, tokens["refresh_token"]))
    assert (a.status_code, b.status_code) == (200, 200), (a.text, b.text)
    ta, tb = a.json(), b.json()
    assert ta["refresh_token"] != tb["refresh_token"]
    # each tab carries on with its own token; neither was signed out
    assert (await refresh(client, ta["refresh_token"])).status_code == 200
    assert (await refresh(client, tb["refresh_token"])).status_code == 200


async def test_a_flood_of_concurrent_replays_is_forgiven_only_up_to_the_limit_then_it_is_treated_as_theft(client, db_session):
    from sqlalchemy import select

    from app.core.config import get_settings
    from app.modules.auth.models.refresh_token import RefreshToken

    tokens = await session_tokens(client)
    limit = get_settings().refresh_reuse_grace_max_replays
    results = await asyncio.gather(*[refresh(client, tokens["refresh_token"]) for _ in range(limit + 3)])
    codes = sorted(r.status_code for r in results)
    # one real rotation, `limit` forgiven replays; the rest look like theft and shut the family down
    assert codes.count(200) == 1 + limit and codes.count(401) == 2, codes
    rows = (await db_session.execute(select(RefreshToken))).scalars().all()
    assert rows and all(row.revoked for row in rows)  # no token survives to fork from


async def test_a_replay_is_recorded_in_the_audit_log(client, db_session):
    tokens = await session_tokens(client)
    assert (await refresh(client, tokens["refresh_token"])).status_code == 200
    assert (await refresh(client, tokens["refresh_token"])).status_code == 200  # the replay
    rows = (await db_session.execute(text("SELECT action, target_type, details FROM audit_logs WHERE action = 'auth.refresh_replay_accepted'"))).all()
    assert len(rows) == 1 and rows[0].target_type == "refresh_token"
    assert rows[0].details["replays"] == 1


async def test_reuse_after_the_window_revokes_the_whole_family(client, db_session):
    tokens = await session_tokens(client)
    child = (await refresh(client, tokens["refresh_token"])).json()
    await db_session.execute(text("UPDATE refresh_tokens SET rotated_at = now() - interval '5 minutes' WHERE revoked"))
    await db_session.commit()
    assert (await refresh(client, tokens["refresh_token"])).status_code == 401  # theft: too late to be a race
    assert (await refresh(client, child["refresh_token"])).status_code == 401  # the family is gone, even the legitimate child


async def test_a_logged_out_session_cannot_be_revived_by_a_replay(client):
    tokens = await session_tokens(client)
    assert (await client.post(f"{API}/auth/logout", json={"refresh_token": tokens["refresh_token"]})).status_code == 204
    assert (await refresh(client, tokens["refresh_token"])).status_code == 401


async def test_existing_tokens_without_a_rotation_time_keep_the_strict_behaviour(client, db_session):
    tokens = await session_tokens(client)
    assert (await refresh(client, tokens["refresh_token"])).status_code == 200
    await db_session.execute(text("UPDATE refresh_tokens SET rotated_at = NULL"))  # what a row from before this feature looks like
    await db_session.commit()
    assert (await refresh(client, tokens["refresh_token"])).status_code == 401
