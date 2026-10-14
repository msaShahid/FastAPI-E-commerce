"""Connection pool configuration and what happens when the pool runs dry."""

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.core.config import Settings, get_settings
from app.core.database import asyncpg_connect_args, build_engine, get_db
from app.main import app


def _settings(test_engine, **overrides) -> Settings:
    url = test_engine.url.render_as_string(hide_password=False)
    return get_settings().model_copy(update={"database_url": url, **overrides})


async def test_pool_limits_come_from_settings(test_engine):
    # One connection, no overflow, 1 s timeout: while that one connection is
    # checked out a second request for one must time out. This fails if the
    # engine ignores the configured values (e.g. hard-coded pool_size=5).
    engine = build_engine(
        _settings(test_engine, db_pool_size=1, db_max_overflow=0, db_pool_timeout=1)
    )
    try:
        async with engine.connect() as held:
            await held.execute(text("SELECT 1"))
            with pytest.raises(PoolTimeoutError):
                async with engine.connect():
                    pass
        # Once the first connection is released, the pool works again.
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        await engine.dispose()


async def test_overflow_connections_are_allowed_up_to_the_limit(test_engine):
    engine = build_engine(
        _settings(test_engine, db_pool_size=1, db_max_overflow=1, db_pool_timeout=1)
    )
    try:
        async with engine.connect() as first, engine.connect() as second:
            await first.execute(text("SELECT 1"))
            await second.execute(text("SELECT 1"))
            with pytest.raises(PoolTimeoutError):
                async with engine.connect():
                    pass
    finally:
        await engine.dispose()


async def test_disabling_prepared_statements_still_works(test_engine):
    engine = build_engine(_settings(test_engine, db_disable_prepared_statements=True))
    try:
        async with engine.connect() as conn:
            for _ in range(3):  # the same statement repeatedly: no cached prepared statement
                assert (await conn.execute(text("SELECT 42"))).scalar_one() == 42
    finally:
        await engine.dispose()


def test_connect_args_are_empty_unless_prepared_statements_are_disabled():
    base = get_settings()
    assert asyncpg_connect_args(base.model_copy(update={"db_disable_prepared_statements": False})) == {}

    args = asyncpg_connect_args(base.model_copy(update={"db_disable_prepared_statements": True}))
    assert args["statement_cache_size"] == 0
    assert args["prepared_statement_cache_size"] == 0
    # Every prepared statement gets a unique name, so two clients sharing one
    # pooled server connection can never collide.
    assert args["prepared_statement_name_func"]() != args["prepared_statement_name_func"]()


@pytest.mark.parametrize(
    "field,value",
    [
        ("db_pool_size", 0),
        ("db_max_overflow", -1),
        ("db_pool_timeout", 0),
        ("db_pool_recycle", -2),
    ],
)
def test_nonsensical_pool_settings_are_rejected(field, value):
    with pytest.raises(ValidationError):
        Settings.model_validate({**get_settings().model_dump(), field: value})


async def test_pool_exhaustion_returns_503_with_retry_after(client):
    # Regression: pool exhaustion used to surface as an unexplained 500.
    async def exhausted_db():
        raise PoolTimeoutError("QueuePool limit of size 5 overflow 10 reached")
        yield  # pragma: no cover  (makes this an async generator like get_db)

    app.dependency_overrides[get_db] = exhausted_db
    try:
        response = await client.get("/api/v1/products")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert "busy" in response.json()["detail"].lower()
