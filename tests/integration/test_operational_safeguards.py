# ruff: noqa: F811  (fixtures imported from the lifecycle suite are re-declared as test parameters)
"""Phase 10: last-admin protection, upload size cap, login timing, and the health endpoints."""

import asyncio
import io

from test_order_payment_lifecycle import API, category  # noqa: F401

from app.core.config import get_settings

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


async def second_admin(client, admin_headers, db_session, name="admin2"):
    from sqlalchemy import text

    await client.post(f"{API}/auth/register", json={"username": name, "email": f"{name}@example.com", "password": "AdminPass123!"})
    await db_session.execute(text("UPDATE users SET role = 'admin' WHERE username = :n"), {"n": name})
    await db_session.commit()
    tok = (await client.post(f"{API}/auth/login", json={"email": f"{name}@example.com", "password": "AdminPass123!"})).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


async def me(client, headers):
    return (await client.get(f"{API}/auth/me", headers=headers)).json()


# ------------------------------------------------------------------ last active administrator
async def test_the_last_active_admin_cannot_be_demoted_or_deactivated(client, admin_headers):
    admin = await me(client, admin_headers)
    demote = await client.patch(f"{API}/users/{admin['id']}", json={"role": "user"}, headers=admin_headers)
    assert demote.status_code == 409 and demote.json()["code"] == "last_admin"
    deactivate = await client.delete(f"{API}/users/{admin['id']}", headers=admin_headers)
    assert deactivate.status_code == 409 and deactivate.json()["code"] == "last_admin"
    assert (await me(client, admin_headers))["role"] == "admin"


async def test_an_admin_can_be_demoted_or_deactivated_when_another_active_admin_exists(client, admin_headers, db_session):
    other = await second_admin(client, admin_headers, db_session)
    first = await me(client, admin_headers)
    assert (await client.patch(f"{API}/users/{first['id']}", json={"role": "user"}, headers=other)).status_code == 200
    # now `other` is the last one again
    other_id = (await me(client, other))["id"]
    assert (await client.delete(f"{API}/users/{other_id}", headers=other)).status_code == 409


async def test_two_admins_demoting_each_other_at_once_cannot_leave_zero_admins(client, admin_headers, db_session):
    from sqlalchemy import text

    other = await second_admin(client, admin_headers, db_session)
    a, b = await me(client, admin_headers), await me(client, other)
    results = await asyncio.gather(
        client.patch(f"{API}/users/{b['id']}", json={"role": "user"}, headers=admin_headers),
        client.patch(f"{API}/users/{a['id']}", json={"role": "user"}, headers=other),
    )
    # One demotion succeeds. The other is refused: 409 (last admin) if it reached the guard first, or 403 if its caller had already
    # been demoted by then. Either way exactly one administrator remains.
    assert sorted(r.status_code for r in results) in ([200, 403], [200, 409])
    await db_session.rollback()
    assert (await db_session.execute(text("SELECT count(*) FROM users WHERE role = 'admin' AND is_active"))).scalar_one() == 1


# ------------------------------------------------------------------ upload size
async def test_an_oversized_image_is_refused_and_one_within_the_limit_is_accepted(client, admin_headers, category, monkeypatch):
    product = (await client.post(f"{API}/products", headers=admin_headers, json={
        "name": "Img", "description": None, "price_cents": 1000, "sku": "IMG-1", "stock": 1, "category_id": category, "status": "active"})).json()
    url = f"{API}/products/{product['id']}/image"
    monkeypatch.setattr(get_settings(), "max_image_bytes", 100)
    big = await client.post(url, headers=admin_headers, files={"image": ("a.png", io.BytesIO(PNG + b"\x00" * 200), "image/png")})
    assert big.status_code == 422 and "too large" in str(big.json())
    ok = await client.post(url, headers=admin_headers, files={"image": ("a.png", io.BytesIO(PNG), "image/png")})
    assert ok.status_code in (200, 201), ok.text


# ------------------------------------------------------------------ login timing
async def test_an_unknown_email_costs_the_same_password_work_as_a_wrong_password(client, monkeypatch):
    from app.modules.auth.services import auth_service

    calls = {"real": 0, "decoy": 0}
    real_verify, real_decoy = auth_service.verify_password, auth_service.verify_against_decoy
    monkeypatch.setattr(auth_service, "verify_password", lambda *a, **k: (calls.__setitem__("real", calls["real"] + 1), real_verify(*a, **k))[1])
    monkeypatch.setattr(auth_service, "verify_against_decoy", lambda *a, **k: (calls.__setitem__("decoy", calls["decoy"] + 1), real_decoy(*a, **k))[1])
    await client.post(f"{API}/auth/register", json={"username": "timing_u", "email": "timing@example.com", "password": "UserPass123!"})
    wrong = await client.post(f"{API}/auth/login", json={"email": "timing@example.com", "password": "nope-nope-nope"})
    unknown = await client.post(f"{API}/auth/login", json={"email": "nobody@example.com", "password": "nope-nope-nope"})
    assert wrong.status_code == unknown.status_code == 401 and wrong.json()["detail"] == unknown.json()["detail"]
    assert calls == {"real": 1, "decoy": 1}  # one full password check either way


# ------------------------------------------------------------------ health
async def test_public_health_says_nothing_about_the_deployment_and_readiness_is_minimal(client):
    assert (await client.get("/health")).json() == {"status": "ok"}
    assert (await client.get("/health/db")).json() == {"status": "ok"}


async def test_readiness_is_503_when_the_database_is_unreachable(client, monkeypatch):
    from app.core.database import get_db
    from app.main import app

    class Broken:
        async def execute(self, *a, **k):
            raise ConnectionError("db down")

    async def broken():
        yield Broken()

    app.dependency_overrides[get_db] = broken
    r = await client.get("/health/db")
    assert r.status_code == 503 and "db down" not in r.text


async def test_dependency_health_is_admin_only_and_reports_each_dependency(client, admin_headers):
    assert (await client.get(f"{API}/admin/health/dependencies")).status_code in (401, 403)
    body = (await client.get(f"{API}/admin/health/dependencies", headers=admin_headers)).json()
    assert body["database"]["ok"] is True and body["redis"]["ok"] is True
    assert body["worker"]["ok"] is False and body["worker"]["last_job_finished_seconds_ago"] is None  # no job has ever run here
    assert body["status"] == "degraded"


async def test_dependency_health_reports_ok_once_the_worker_has_finished_a_job(client, admin_headers, db_session):
    from sqlalchemy import text

    await db_session.execute(text("INSERT INTO background_jobs (job_type, payload, max_attempts, run_at, status, finished_at) "
                                  "VALUES ('noop', '{}'::jsonb, 3, now(), 'succeeded', now())"))
    await db_session.commit()
    body = (await client.get(f"{API}/admin/health/dependencies", headers=admin_headers)).json()
    assert body["worker"]["ok"] is True and body["worker"]["last_job_finished_seconds_ago"] <= 5
    assert body["status"] == "ok"


async def test_dependency_health_reports_a_stalled_worker(client, admin_headers, db_session):
    from sqlalchemy import text

    await db_session.execute(text("INSERT INTO background_jobs (job_type, payload, max_attempts, run_at, status, finished_at) "
                                  "VALUES ('noop', '{}'::jsonb, 3, now(), 'succeeded', now() - interval '2 hours')"))
    await db_session.commit()
    body = (await client.get(f"{API}/admin/health/dependencies", headers=admin_headers)).json()
    assert body["worker"]["ok"] is False and body["status"] == "degraded"


async def test_the_last_admin_check_serialises_concurrent_requests_on_the_admin_rows(client, admin_headers, test_session_factory):
    """Two requests that both pass authorisation must still decide ONE AT A TIME whether another admin remains."""
    from app.modules.users.repositories.user_repository import UserRepository

    async with test_session_factory() as first, test_session_factory() as second:
        assert len(await UserRepository(first).lock_active_admins()) == 1  # `first` now holds the lock inside its transaction
        blocked = asyncio.ensure_future(UserRepository(second).lock_active_admins())
        done, _pending = await asyncio.wait({blocked}, timeout=0.7)
        assert not done  # the second check is WAITING, not reading a stale count
        await first.commit()  # the first request finishes ...
        assert len(await asyncio.wait_for(blocked, timeout=5)) == 1  # ... and only then does the second one see the result
