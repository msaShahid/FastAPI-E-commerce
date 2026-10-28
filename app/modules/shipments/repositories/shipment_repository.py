import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.shipments.exceptions.shipment_exceptions import DuplicateTrackingError
from app.modules.shipments.models.order_shipment import OrderShipment


class ShipmentRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, order_id: uuid.UUID, shipment_id: uuid.UUID) -> OrderShipment | None:
        return (
            await self.db.execute(select(OrderShipment).where(OrderShipment.id == shipment_id, OrderShipment.order_id == order_id))
        ).scalar_one_or_none()

    async def add(self, shipment: OrderShipment) -> OrderShipment:
        try:
            async with self.db.begin_nested():
                self.db.add(shipment)
                await self.db.flush()
        except IntegrityError as exc:
            raise DuplicateTrackingError() from exc
        return shipment

    async def save(self, shipment: OrderShipment) -> None:
        try:
            async with self.db.begin_nested():
                await self.db.flush()
        except IntegrityError as exc:
            raise DuplicateTrackingError() from exc

    async def delete(self, shipment: OrderShipment) -> None:
        await self.db.delete(shipment)
        await self.db.flush()
