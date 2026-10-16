"""Background jobs started by payments."""

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.email import get_email_sender
from app.core.jobs.registry import job_handler
from app.modules.auth.models.user import User
from app.modules.orders.repositories.order_repository import OrderRepository

logger = logging.getLogger(__name__)

SEND_ORDER_CONFIRMATION = "send_order_confirmation"


def order_confirmation_dedupe_key(order_id: UUID) -> str:
    return f"order-confirmation:{order_id}"


@job_handler(SEND_ORDER_CONFIRMATION)
async def send_order_confirmation(
    payload: dict, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """
    Emails the customer that their payment was received. Queued by the Stripe
    webhook in the same transaction that marks the order PAID.

    Idempotent enough: running twice would send two emails, which is why the
    job is queued with a dedupe key and why this only reads the database.
    """
    order_id = UUID(payload["order_id"])

    async with session_factory() as session:
        order = await OrderRepository(session).get_by_id(order_id)
        if order is None:
            logger.warning("order %s no longer exists; no confirmation sent", order_id)
            return
        if order.guest_email:
            recipient = order.guest_email
        else:
            user = await session.get(User, order.user_id)
            if user is None:
                logger.warning("owner of order %s no longer exists; nothing sent", order_id)
                return
            recipient = user.email
        lines = [f"  {item.quantity} x {item.product_name_snapshot}" for item in order.items]
        total = f"{order.total_cents / 100:.2f}"
        short_id = str(order.id)[:8]

    body = "\n".join(
        [
            "Thank you -- we have received your payment.",
            "",
            f"Order {short_id}",
            *lines,
            "",
            f"Total paid: {total}",
        ]
    )
    await get_email_sender().send(
        to=recipient, subject=f"Payment received for order {short_id}", body=body
    )
