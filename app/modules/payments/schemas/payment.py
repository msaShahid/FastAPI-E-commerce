from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

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
    """A refund's lifecycle. `requested` means we asked; only `succeeded` is provider-confirmed."""

    id: UUID
    payment_id: UUID
    order_id: UUID
    status: str
    amount_cents: int
    currency: str
    provider_refund_id: str | None
    attempts: int
    failure_reason: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ReconciliationRead(BaseModel):
    id: UUID
    kind: str
    status: str
    payment_id: UUID | None
    order_id: UUID | None
    provider_payment_id: str | None
    details: dict
    resolution: str | None
    resolution_note: str | None
    resolved_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ReconciliationResolve(BaseModel):
    resolution: Literal["handled_externally", "no_action_needed"]
    note: str = Field(min_length=3, max_length=1000, description="What was done / why no action is needed")


class LifecycleHealth(BaseModel):
    """Is the order/payment lifecycle healthy? Read-only."""

    stale_pending_orders: int
    oldest_pending_age_seconds: int | None
    open_reconciliations: int
    active_refunds: dict[str, int]
    failed_jobs: int
    sweep_interval_seconds: int
    last_sweep_succeeded_at: datetime | None
    sweep_overdue: bool


class WebhookEventRead(BaseModel):
    """A provider webhook we accepted, and what happened to it. No payload is stored."""

    provider: str
    event_id: str
    event_type: str | None
    status: str
    attempts: int
    last_error: str | None
    processed_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class DisputeRead(BaseModel):
    id: UUID
    provider: str
    provider_dispute_id: str
    payment_id: UUID
    order_id: UUID
    amount_cents: int
    currency: str
    status: str
    provider_status: str | None
    reason: str | None
    closed_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}
