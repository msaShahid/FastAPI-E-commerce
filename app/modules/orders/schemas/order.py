from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from app.shared.enums.order_status import OrderStatus


class OrderItemRead(BaseModel):
    id: int
    product_id: int
    product_name_snapshot: str
    price_cents_snapshot: int
    quantity: int
    line_total_cents: int

    model_config = {"from_attributes": True}


class OrderRead(BaseModel):
    id: UUID
    status: OrderStatus
    subtotal_cents: int
    shipping_cents: int
    tax_cents: int
    total_cents: int
    items: list[OrderItemRead]
    created_at: datetime

    model_config = {"from_attributes": True}


class OrderStatusUpdate(BaseModel):
    status: OrderStatus