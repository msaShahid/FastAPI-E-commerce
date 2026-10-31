"""Recording shipments against orders. An administrator enters the carrier, tracking number and link by hand: there is no courier
integration, and nothing here changes what a customer is charged. The order's STATUS stays governed by the existing transition rules:
recording a shipment leaves it alone unless the caller explicitly asks to fulfil the order, and then it goes through the same rules and
history as any other status change."""

import uuid
from datetime import UTC, datetime

from app.modules.audit.services.audit_service import AuditService
from app.modules.auth.models.user import User
from app.modules.invoices.services.invoice_service import issue_invoice
from app.modules.orders.exceptions.order_exceptions import (
    InvalidStatusTransitionError,
    OrderNotFoundError,
)
from app.modules.orders.repositories.order_repository import OrderRepository
from app.modules.shipments.exceptions.shipment_exceptions import (
    InvalidShipmentStatusChangeError,
    OrderNotShippableError,
    ShipmentNotFoundError,
)
from app.modules.shipments.models.order_shipment import OrderShipment
from app.modules.shipments.repositories.shipment_repository import ShipmentRepository
from app.modules.shipments.schemas.shipment import ShipmentCreate, ShipmentUpdate
from app.shared.enums.order_status import ALLOWED_TRANSITIONS, OrderStatus

SHIPPABLE = (OrderStatus.PAID, OrderStatus.FULFILLED)


class ShipmentService:
    def __init__(self, shipments: ShipmentRepository, orders: OrderRepository, audit: AuditService) -> None:
        self.shipments = shipments
        self.orders = orders
        self.audit = audit

    async def _locked_order(self, order_id: uuid.UUID):
        order = await self.orders.get_by_id_for_update(order_id)  # serialises with refunds, cancellation and status changes
        if order is None:
            raise OrderNotFoundError(order_id)
        return order

    async def create(self, order_id: uuid.UUID, payload: ShipmentCreate, admin: User) -> OrderShipment:
        order = await self._locked_order(order_id)
        if order.status not in SHIPPABLE:
            raise OrderNotShippableError(order.status.value)
        shipment = await self.shipments.add(OrderShipment(
            order_id=order.id, carrier_name=payload.carrier_name, tracking_number=payload.tracking_number,
            tracking_url=payload.tracking_url, created_by_user_id=admin.id,
        ))
        fulfilled = False
        if payload.fulfill_order and order.status == OrderStatus.PAID:
            if OrderStatus.FULFILLED not in ALLOWED_TRANSITIONS[order.status]:  # the rules decide, not this service
                raise InvalidStatusTransitionError(order.status.value, OrderStatus.FULFILLED.value)
            await self.orders.update_status(order, OrderStatus.FULFILLED, admin.id)
            await issue_invoice(self.orders.db, order)  # a GST order must not ship without its tax invoice
            fulfilled = True
        await self.audit.log(
            actor_user_id=admin.id, action="shipment.created", target_type="order", target_id=order.id,
            details={"shipment_id": str(shipment.id), "carrier": payload.carrier_name, "has_tracking_number": bool(payload.tracking_number),
                     "has_tracking_url": bool(payload.tracking_url), "order_fulfilled": fulfilled},
        )
        return shipment

    async def update(self, order_id: uuid.UUID, shipment_id: uuid.UUID, payload: ShipmentUpdate, admin: User) -> OrderShipment:
        await self._locked_order(order_id)
        shipment = await self.shipments.get(order_id, shipment_id)
        if shipment is None:
            raise ShipmentNotFoundError()
        sent = payload.model_fields_set
        if "carrier_name" in sent:
            shipment.carrier_name = payload.carrier_name
        if "tracking_number" in sent:
            shipment.tracking_number = payload.tracking_number
        if "tracking_url" in sent:
            shipment.tracking_url = payload.tracking_url
        if "status" in sent and payload.status is not None and payload.status != shipment.status:
            if (shipment.status, payload.status) != ("shipped", "delivered"):
                raise InvalidShipmentStatusChangeError(shipment.status, payload.status)
            shipment.status = "delivered"
            shipment.delivered_at = datetime.now(UTC)
        await self.shipments.save(shipment)
        await self.audit.log(
            actor_user_id=admin.id, action="shipment.updated", target_type="order", target_id=order_id,
            details={"shipment_id": str(shipment.id), "fields": sorted(sent), "status": shipment.status},
        )
        return shipment

    async def delete(self, order_id: uuid.UUID, shipment_id: uuid.UUID, admin: User) -> None:
        await self._locked_order(order_id)
        shipment = await self.shipments.get(order_id, shipment_id)
        if shipment is None:
            raise ShipmentNotFoundError()
        details = {"shipment_id": str(shipment.id), "carrier": shipment.carrier_name, "had_tracking_number": bool(shipment.tracking_number)}
        await self.shipments.delete(shipment)
        await self.audit.log(actor_user_id=admin.id, action="shipment.deleted", target_type="order", target_id=order_id, details=details)
