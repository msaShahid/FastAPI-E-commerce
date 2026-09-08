import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import Base, get_db
from app.core.storage.interface import StorageService
from app.main import app

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
            for table in reversed(Base.metadata.sorted_tables):
                await conn.execute(table.delete())

    await clean()
    yield
    await clean()




@pytest_asyncio.fixture
async def db_session(test_session_factory, _clean_database):
    """For tests that want to query the database directly, not just through HTTP."""
    async with test_session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def client(test_session_factory, _clean_database):
    """
    override_get_db mirrors core/database.py's real get_db (commit on
    success, rollback on failure, always close). Every request gets a
    fresh, independent session, same as production.
    """

    async def override_get_db():
        async with test_session_factory() as session:
            try:
                yield session
                await session.commit()
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