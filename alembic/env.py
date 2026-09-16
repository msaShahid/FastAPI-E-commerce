import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.core.config import get_settings
from app.core.database import Base

# Import every model module here so Base.metadata knows about its table.
from app.modules.auth.models.refresh_token import RefreshToken  # noqa: F401
from app.modules.auth.models.user import User  # noqa: F401
from app.modules.cart.models.cart import Cart  # noqa: F401
from app.modules.cart.models.cart_item import CartItem  # noqa: F401
from app.modules.categories.models.category import Category  # noqa: F401
from app.modules.orders.models.order import Order  # noqa: F401
from app.modules.orders.models.order_item import OrderItem  # noqa: F401
from app.modules.orders.models.order_status_history import OrderStatusHistory  # noqa: F401
from app.modules.products.models.product import Product  # noqa: F401

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Use our application's own settings as the single source of truth for
# the database URL, instead of hardcoding it separately in alembic.ini
# -- one connection string to keep correct, not two.
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

# Point autogenerate at our real models' metadata.
target_metadata = Base.metadata



def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
