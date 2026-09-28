"""
Releases stock held by abandoned checkouts.

Checkout decrements stock immediately when an order is created (see
OrderService.checkout), and an order only ever reaches PAID through a
Stripe webhook. If a customer starts checkout and never completes
payment, that order sits in PENDING forever and its stock is never
released. This script finds every order that has been PENDING longer
than settings.pending_order_timeout_minutes, cancels it, and restocks
its items -- each order in its own row-locked transaction, so it's
safe to run concurrently with real traffic (a customer paying at the
exact moment this runs just wins the race; see
OrderService.cancel_stale_pending_orders for how).

This project has no task queue (no Celery/Redis -- see the technical
spec's stack notes), so this is meant to be invoked periodically by an
external scheduler rather than run as a long-lived worker, e.g. a cron
entry:

    */5 * * * * cd /path/to/app && python -m scripts.cancel_stale_orders

or, in Docker Compose, a sibling one-shot service on its own cron/timer
(there is no such service defined yet -- add one if you deploy this).

Run manually with:
    docker compose exec api python -m scripts.cancel_stale_orders
"""

import asyncio
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.core.database import async_session_factory
from app.modules.cart.repositories.cart_repository import CartRepository
from app.modules.orders.repositories.order_repository import OrderRepository
from app.modules.orders.services.order_service import OrderService
from app.modules.products.repositories.product_repository import ProductRepository

settings = get_settings()


async def main() -> None:
    # orders.created_at is a naive DateTime column (TimestampMixin doesn't
    # set timezone=True, unlike e.g. refresh_tokens.expires_at), populated
    # from Postgres's now() -- so the cutoff must be naive too, or both the
    # SQL comparison and OrderService's in-Python re-check under the lock
    # break comparing aware vs. naive datetimes. By convention (matching
    # how the rest of this column is used) it's naive-but-UTC.
    cutoff = (datetime.now(UTC) - timedelta(minutes=settings.pending_order_timeout_minutes)).replace(
        tzinfo=None
    )

    async with async_session_factory() as db:
        order_service = OrderService(
            OrderRepository(db),
            CartRepository(db),
            ProductRepository(db),
        )

        cancelled_order_ids = await order_service.cancel_stale_pending_orders(
            older_than=cutoff
        )
        await db.commit()

    if cancelled_order_ids:
        print(f"Cancelled {len(cancelled_order_ids)} stale order(s):")
        for order_id in cancelled_order_ids:
            print(f"  - {order_id}")
    else:
        print("No stale orders to cancel.")


if __name__ == "__main__":
    asyncio.run(main())
