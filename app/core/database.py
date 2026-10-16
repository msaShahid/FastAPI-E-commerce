import uuid
from collections.abc import AsyncGenerator
from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.cache import flush_pending
from app.core.config import Settings, get_settings

settings = get_settings()



def asyncpg_connect_args(config: Settings) -> dict:
    """
    Extra asyncpg arguments. Empty unless DB_DISABLE_PREPARED_STATEMENTS is set.

    Used by both the application engine and Alembic's engine, so migrations
    and `alembic check` behave the same way as the app behind a pooler.
    """
    if not config.db_disable_prepared_statements:
        return {}
    # SQLAlchemy's documented setup for asyncpg behind a transaction-mode
    # PgBouncer: no statement caches, and unique statement names so two
    # clients sharing one server connection can never collide.
    return {
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
        "prepared_statement_name_func": lambda: f"__asyncpg_{uuid.uuid4()}__",
    }


def build_engine(config: Settings) -> AsyncEngine:
    """
    Creates the async engine with the configured connection pool.

    pool_pre_ping: a connection that the server closed while it sat idle
    (database restart, a provider that scales to zero, a firewall timeout) is
    detected and replaced before use, instead of failing the request.
    pool_recycle: connections are replaced after a while so none lives forever.
    """
    return create_async_engine(
        config.database_url,
        echo=config.environment == "development",
        pool_size=config.db_pool_size,
        max_overflow=config.db_max_overflow,
        pool_timeout=config.db_pool_timeout,
        pool_recycle=config.db_pool_recycle,
        pool_pre_ping=config.db_pool_pre_ping,
        connect_args=asyncpg_connect_args(config),
    )


engine = build_engine(settings)

async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
            # Only after the commit: invalidating earlier would let a reader
            # re-fill the cache from the old, still-committed data.
            await flush_pending(session)
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


DbSession = Annotated[
    AsyncSession,
    Depends(get_db),
]
