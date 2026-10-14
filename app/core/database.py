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

from app.core.config import Settings, get_settings

settings = get_settings()



def asyncpg_connect_args(config: Settings) -> dict:

    if not config.db_disable_prepared_statements:
        return {}
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
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


DbSession = Annotated[
    AsyncSession,
    Depends(get_db),
]
