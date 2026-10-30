import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.core.config import get_settings
from app.core.database import Base, asyncpg_connect_args

# Import every model module here so Base.metadata knows about its table.
from app.core.jobs.models import BackgroundJob  # noqa: F401
from app.modules.addresses.models.address import Address  # noqa: F401
from app.modules.audit.models.audit_log import AuditLog  # noqa: F401
from app.modules.auth.models.auth_token import AuthToken  # noqa: F401
from app.modules.auth.models.refresh_token import RefreshToken  # noqa: F401
from app.modules.auth.models.user import User  # noqa: F401
from app.modules.cart.models.cart import Cart  # noqa: F401
from app.modules.cart.models.cart_item import CartItem  # noqa: F401
from app.modules.categories.models.category import Category  # noqa: F401
from app.modules.email_settings.models.email_provider_config import EmailProviderConfig  # noqa: F401
from app.modules.orders.models.currency_remediation import OrderCurrencyRemediation  # noqa: F401
from app.modules.orders.models.order import Order  # noqa: F401
from app.modules.orders.models.order_item import OrderItem  # noqa: F401
from app.modules.orders.models.order_item_restock import OrderItemRestock  # noqa: F401
from app.modules.orders.models.order_status_history import OrderStatusHistory  # noqa: F401
from app.modules.payments.models.deferred_event import DeferredWebhookEvent  # noqa: F401
from app.modules.payments.models.dispute import Dispute  # noqa: F401
from app.modules.payments.models.payment import Payment  # noqa: F401
from app.modules.payments.models.provider_config import PaymentProviderConfig  # noqa: F401
from app.modules.payments.models.refund import PaymentReconciliation, Refund  # noqa: F401
from app.modules.payments.models.webhook_event import ProcessedWebhookEvent  # noqa: F401
from app.modules.products.models.product import Product  # noqa: F401
from app.modules.products.models.stock_movement import StockMovement  # noqa: F401
from app.modules.products.models.variants import (  # noqa: F401
    ProductOption,
    ProductOptionValue,
    ProductVariant,
    SkuRegistry,
)
from app.modules.reviews.models.product_review import ProductReview  # noqa: F401
from app.modules.shipments.models.order_shipment import OrderShipment  # noqa: F401
from app.modules.store.models.store_settings import StoreSettings  # noqa: F401
from app.modules.tax.models.tax import (  # noqa: F401
    BusinessTaxSettings,
    GstRegistration,
    TaxClassification,
    TaxRule,
)
from app.modules.wishlist.models.wishlist_item import WishlistItem  # noqa: F401

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


# Downgrades are a DEVELOPMENT convenience (see docs/MIGRATIONS.md): several drop columns or tables outright. They are refused on a
# database that holds real data unless the operator says so explicitly: `alembic -x allow_data_loss=true downgrade ...`.
# Production recovery is "restore the backup", never "downgrade".
DOWNGRADE_PROTECTED_TABLES = ("users", "products", "orders", "payments")
_downgrade_checked = False


def refuse_downgrade_with_data(ctx, step, heads, run_args) -> None:
    """Runs after each step, inside the same transaction as the whole run, so raising here rolls EVERYTHING back."""
    global _downgrade_checked
    if _downgrade_checked or step.is_upgrade:
        return
    _downgrade_checked = True
    if context.get_x_argument(as_dictionary=True).get("allow_data_loss", "").lower() == "true":
        return
    for table in DOWNGRADE_PROTECTED_TABLES:
        exists = ctx.connection.exec_driver_sql(f"SELECT to_regclass('public.{table}') IS NOT NULL").scalar()
        if exists and ctx.connection.exec_driver_sql(f"SELECT EXISTS (SELECT 1 FROM {table})").scalar():
            raise RuntimeError(
                f"Refusing to downgrade: table '{table}' holds data and a downgrade can destroy it. Nothing was changed. "
                "To recover production, restore a backup. To downgrade a throwaway database anyway: "
                "alembic -x allow_data_loss=true downgrade <revision>"
            )


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, on_version_apply=refuse_downgrade_with_data
    )

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
        # Same prepared-statement handling as the app (a no-op unless
        # DB_DISABLE_PREPARED_STATEMENTS=true), so migrations and
        # `alembic check` also work behind a PgBouncer-style pooler.
        connect_args=asyncpg_connect_args(settings),
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
