from uuid import UUID

from pydantic import BaseModel

from app.shared.enums.payment_status import PaymentStatus


class PaymentCreate(BaseModel):
    order_id: UUID


class PaymentRead(BaseModel):
    id: UUID
    order_id: UUID
    status: PaymentStatus
    amount_cents: int
    currency: str
    provider: str
    client_secret: str | None = None

    model_config = {"from_attributes": True}


class RefundRead(BaseModel):
    payment_id: UUID
    order_id: UUID
    status: PaymentStatus
    refunded_amount_cents: int
    provider_refund_id: str

    model_config = {"from_attributes": True}