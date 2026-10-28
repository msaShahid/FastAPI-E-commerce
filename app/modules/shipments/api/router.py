"""Administrator endpoints to record and correct an order's shipments. Customers do not call these: they see their shipments inside
the order itself (`shipments` on GET /orders/{id}, the order list and the guest order view)."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.core.database import DbSession
from app.modules.audit.dependencies.audit_deps import AuditServiceDep
from app.modules.auth.dependencies.auth import AdminUser
from app.modules.orders.repositories.order_repository import OrderRepository
from app.modules.shipments.repositories.shipment_repository import ShipmentRepository
from app.modules.shipments.schemas.shipment import ShipmentCreate, ShipmentRead, ShipmentUpdate
from app.modules.shipments.services.shipment_service import ShipmentService

shipment_router = APIRouter(prefix="/orders", tags=["shipments"])


def get_shipment_service(db: DbSession, audit: AuditServiceDep) -> ShipmentService:
    return ShipmentService(ShipmentRepository(db), OrderRepository(db), audit)


ServiceDep = Annotated[ShipmentService, Depends(get_shipment_service)]


@shipment_router.post("/{order_id}/shipments", response_model=ShipmentRead, status_code=status.HTTP_201_CREATED)
async def create_shipment(order_id: UUID, payload: ShipmentCreate, admin: AdminUser, service: ServiceDep):
    """Record a parcel handed to a carrier. Allowed for PAID or FULFILLED orders. Optional `fulfill_order: true` also marks a PAID order
    FULFILLED. The tracking number and link may be added later."""
    return await service.create(order_id, payload, admin)


@shipment_router.patch("/{order_id}/shipments/{shipment_id}", response_model=ShipmentRead)
async def update_shipment(order_id: UUID, shipment_id: UUID, payload: ShipmentUpdate, admin: AdminUser, service: ServiceDep):
    """Correct the carrier, add or clear the tracking number or link, or mark the parcel delivered."""
    return await service.update(order_id, shipment_id, payload, admin)


@shipment_router.delete("/{order_id}/shipments/{shipment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_shipment(order_id: UUID, shipment_id: UUID, admin: AdminUser, service: ServiceDep) -> None:
    """Remove a shipment entered by mistake (the audit log keeps a record of it)."""
    await service.delete(order_id, shipment_id, admin)
