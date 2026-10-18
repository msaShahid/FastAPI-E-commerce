import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.cache import Cache, flush_pending, set_cache
from app.core.database import Base, get_db
from app.core.storage.interface import StorageService
from app.main import app
from app.modules.tax.models.guard import GUARDED_TABLES

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://test_user:test_password@test_db:5432/shop_test_db",
)


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    """Runs once per test session: create every table, drop them all at the very end."""
    engine = create_async_engine(TEST_DATABASE_URL)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()


@pytest_asyncio.fixture(scope="session")
def test_session_factory(test_engine):
    """
    A completely ordinary session factory bound to the engine. Every
    session created from this gets its own real connection checked out
    of the pool, exactly the same way core/database.py's get_db works
    for the real app.
    """
    return async_sessionmaker(bind=test_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def _clean_database(test_engine):
    async def clean():
        async with test_engine.begin() as conn:
            # The GST master's immutability trigger (rightly) refuses DELETE of approved
            # records. TRUNCATE does not fire row triggers, so it is the test-cleanup path.
            # CASCADE also empties products (they reference classifications) and their dependents.
            # store_settings is truncated too: its guard refuses DELETE once orders exist.
            await conn.execute(text(
                "TRUNCATE business_tax_settings, tax_rules, tax_classifications, gst_registrations, "
                "store_settings CASCADE"))
            for table in reversed(Base.metadata.sorted_tables):
                if table.name not in GUARDED_TABLES and table.name != "store_settings":
                    await conn.execute(table.delete())

    async def seed_store_currency():
        # Production starts with NO store currency (checkout answers 503 until an admin sets it).
        # Tests run with an explicit one; tests about the unconfigured state delete this row.
        async with test_engine.begin() as conn:
            await conn.execute(text("INSERT INTO store_settings (id, currency) VALUES (1, 'usd')"))

    await clean()
    await seed_store_currency()
    yield
    await clean()




@pytest.fixture(autouse=True)
def _checkout_pricing_configured_for_tests(monkeypatch):
    """
    Production leaves shipping/tax UNSET so checkout fails closed. Tests that are
    not about shipping/tax run with an explicit zero config; tests about the
    fail-closed behaviour or real amounts override these on the same object.
    """
    from decimal import Decimal

    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "flat_shipping_cents", 0)
    monkeypatch.setattr(settings, "free_shipping_threshold_cents", None)
    monkeypatch.setattr(settings, "tax_rate_percent", Decimal("0"))
    # The (fake) payment provider in tests may charge any verified currency.
    monkeypatch.setattr(settings, "payment_currencies_enabled", "usd,inr,eur,gbp,jpy")


@pytest.fixture(autouse=True)
def _cache_disabled_by_default():
    """
    Every test starts with caching OFF, so a response cached by one test can
    never leak into another and the existing tests keep exercising the
    database. Tests that are about the cache build their own enabled Cache
    (see tests/integration/test_cache_integration.py).
    """
    set_cache(Cache.disabled())
    yield
    set_cache(None)


@pytest_asyncio.fixture
async def db_session(test_session_factory, _clean_database):
    """For tests that want to query the database directly, not just through HTTP."""
    async with test_session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def client(test_session_factory, _clean_database):

    from app.core.rate_limit import limiter

    limiter.reset()

    async def override_get_db():
        async with test_session_factory() as session:
            try:
                yield session
                await session.commit()
                await flush_pending(session)  # same as get_db
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


class FakeStorageService(StorageService):
    """Trivial in-memory stand-in for StorageService, used by fake-repository unit tests."""

    async def upload(self, *, file, path: str, content_type: str) -> str:
        return path

    async def delete(self, *, path: str) -> None:
        pass

    def get_url(self, *, path: str) -> str:
        return f"/fake-media/{path}"


@pytest.fixture
def fake_storage_service() -> FakeStorageService:
    return FakeStorageService()