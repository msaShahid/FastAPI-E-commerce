from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

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
    cancels_order: bool = False
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


class UnknownCurrencyPayment(BaseModel):
    provider: str
    provider_payment_id: str
    status: str
    amount_cents: int
    currency: str


class CurrencyReviewRead(BaseModel):
    decision: str
    currency: str | None
    evidence_type: str
    evidence_reference: str
    reviewer_user_id: UUID | None
    reviewed_at: datetime
    remediation: str | None
    note: str | None


class UnknownCurrencyOrderRead(BaseModel):
    """A legacy order whose currency is unknown. Payments and refunds are blocked until it is established."""

    order_id: UUID
    status: str
    total_cents: int
    created_at: datetime
    payments: list[UnknownCurrencyPayment]
    manual_review_required: bool
    reviews: list[CurrencyReviewRead]
    caveat: str


class EstablishCurrencyRequest(BaseModel):
    """A reviewer's decision on a legacy order. `established` needs a currency and evidence that demonstrably concerns
    THIS order (a provider record must cite one of its payment ids; an accounting record must cite its order id or a
    payment id; store history is accepted only when attested unambiguous and consistent). `unresolved` records that the
    evidence conflicts or is inconclusive: the currency stays unset and the order needs manual review."""

    decision: Literal["established", "unresolved"] = "established"
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    evidence_type: Literal["provider_record", "accounting_record", "store_history_reviewed"]
    evidence_reference: str = Field(min_length=3, max_length=2000, description="Where the evidence can be checked")
    note: str | None = Field(default=None, max_length=1000)
    reason: Literal["conflicting", "inconclusive"] | None = None
    store_history_unambiguous: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> "EstablishCurrencyRequest":
        if self.decision == "established" and not self.currency:
            raise ValueError("a currency is required to establish it")
        if self.decision == "unresolved":
            if self.currency:
                raise ValueError("an unresolved review must not carry a currency")
            if self.reason is None or not (self.note and len(self.note.strip()) >= 3):
                raise ValueError("an unresolved review needs a reason (conflicting or inconclusive) and a written note")
        return self


class EstablishCurrencyResult(BaseModel):
    order_id: UUID
    decision: str
    currency: str | None
    evidence_type: str
    evidence_reference: str
    remediation: str | None
    reviewer_user_id: UUID | None = None
    reviewed_at: datetime | None = None


class DeferredEventRead(BaseModel):
    """A verified event kept for bounded recovery. Identifiers only: no payload is stored."""

    provider: str
    event_id: str
    event_type: str
    provider_payment_id: str
    status: str
    attempts: int
    next_attempt_at: datetime
    expires_at: datetime
    last_reason: str | None
    resolved_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}
