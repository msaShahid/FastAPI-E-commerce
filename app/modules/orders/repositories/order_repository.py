from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.orders.models.order import Order
from app.modules.orders.models.order_item import OrderItem
from app.modules.orders.models.order_status_history import OrderStatusHistory
from app.shared.enums.order_status import OrderStatus


class OrderRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_idempotency_key(
        self, key: str, *, user_id: UUID | None = None, guest_token: str | None = None
    ) -> Order | None:

        identity_condition = (
            Order.user_id == user_id
            if user_id is not None
            else Order.guest_token == guest_token
        )
        result = await self.db.execute(
            select(Order)
            .where(Order.idempotency_key == key, identity_condition)
            .options(selectinload(Order.items))
        )
        return result.scalar_one_or_none()

    async def get_by_guest_token(self, order_id, *, guest_token: str) -> Order | None:
        """
        A guest's own order lookup -- matched by guest_token instead of
        a user_id ownership check, since a guest order has no user_id
        at all. Never exposed to a caller who doesn't already have the
        exact token from their own checkout (see the guest_cart_token
        cookie / CartOwner).
        """
        result = await self.db.execute(
            select(Order)
            .where(Order.id == order_id, Order.guest_token == guest_token)
            .options(selectinload(Order.items))
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, order_id) -> Order | None:
        result = await self.db.execute(
            select(Order).where(Order.id == order_id).options(selectinload(Order.items))
        )
        return result.scalar_one_or_none()

    async def get_by_id_for_update(self, order_id) -> Order | None:

        result = await self.db.execute(
            select(Order)
            .where(Order.id == order_id)
            .options(selectinload(Order.items))
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def list_stale_pending_order_ids(self, *, older_than: datetime) -> list[UUID]:
        """
        Order ids still PENDING after `older_than` -- candidates for the
        stale-order reaper. Deliberately just ids: the reaper re-fetches
        and locks each one individually rather than holding a lock on
        every stale order for the whole batch.
        """
        result = await self.db.execute(
            select(Order.id).where(
                Order.status == OrderStatus.PENDING,
                Order.created_at < older_than,
            )
        )
        return list(result.scalars().all())

    async def list_for_user(
        self, user_id: UUID, *, offset: int, limit: int
    ) -> tuple[list[Order], int]:
        total_result = await self.db.execute(
            select(func.count()).select_from(Order).where(Order.user_id == user_id)
        )
        total = total_result.scalar_one()

        items_result = await self.db.execute(
            select(Order)
            .where(Order.user_id == user_id)
            .options(selectinload(Order.items))
            .order_by(Order.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(items_result.scalars().all()), total

    async def list_all(
        self, *, offset: int, limit: int, status: OrderStatus | None = None
    ) -> tuple[list[Order], int]:

        conditions = []
        if status is not None:
            conditions.append(Order.status == status)

        total_result = await self.db.execute(
            select(func.count()).select_from(Order).where(*conditions)
        )
        total = total_result.scalar_one()

        items_result = await self.db.execute(
            select(Order)
            .where(*conditions)
            .options(selectinload(Order.items))
            .order_by(Order.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(items_result.scalars().all()), total

    async def create_order(
        self,
        *,
        user_id: UUID | None,
        idempotency_key: str,
        items: list[dict],
        subtotal_cents: int,
        shipping_cents: int,
        tax_cents: int,
        total_cents: int,
        guest_token: str | None = None,
        guest_email: str | None = None,
        shipping_recipient_name: str | None = None,
        shipping_line1: str | None = None,
        shipping_line2: str | None = None,
        shipping_city: str | None = None,
        shipping_state: str | None = None,
        shipping_postal_code: str | None = None,
        shipping_country: str | None = None,
    ) -> Order:
        order = Order(
            user_id=user_id,
            guest_token=guest_token,
            guest_email=guest_email,
            idempotency_key=idempotency_key,
            subtotal_cents=subtotal_cents,
            shipping_cents=shipping_cents,
            tax_cents=tax_cents,
            total_cents=total_cents,
            shipping_recipient_name=shipping_recipient_name,
            shipping_line1=shipping_line1,
            shipping_line2=shipping_line2,
            shipping_city=shipping_city,
            shipping_state=shipping_state,
            shipping_postal_code=shipping_postal_code,
            shipping_country=shipping_country,
        )
        self.db.add(order)
        await self.db.flush()

        for item_data in items:
            self.db.add(OrderItem(order_id=order.id, **item_data))

        self.db.add(
            OrderStatusHistory(
                order_id=order.id,
                from_status=OrderStatus.PENDING,
                to_status=OrderStatus.PENDING,
            )
        )

        await self.db.flush()
        await self.db.refresh(order, attribute_names=["items"])
        return order

    async def update_status(
        self, order: Order, new_status: OrderStatus, changed_by_user_id: UUID | None
    ) -> Order:
        old_status = order.status
        order.status = new_status
        self.db.add(
            OrderStatusHistory(
                order_id=order.id,
                from_status=old_status,
                to_status=new_status,
                changed_by_user_id=changed_by_user_id,
            )
        )
        await self.db.flush()
        return order
