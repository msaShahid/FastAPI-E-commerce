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

    async def get_by_idempotency_key(self, key: str) -> Order | None:
        result = await self.db.execute(
            select(Order)
            .where(Order.idempotency_key == key)
            .options(selectinload(Order.items))
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, order_id) -> Order | None:
        result = await self.db.execute(
            select(Order).where(Order.id == order_id).options(selectinload(Order.items))
        )
        return result.scalar_one_or_none()

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

    async def create_order(
        self,
        *,
        user_id: UUID,
        idempotency_key: str,
        items: list[dict],
        subtotal_cents: int,
        shipping_cents: int,
        tax_cents: int,
        total_cents: int,
    ) -> Order:
        order = Order(
            user_id=user_id,
            idempotency_key=idempotency_key,
            subtotal_cents=subtotal_cents,
            shipping_cents=shipping_cents,
            tax_cents=tax_cents,
            total_cents=total_cents,
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
