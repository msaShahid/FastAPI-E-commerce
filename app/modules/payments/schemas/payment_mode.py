from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class GatewaySummary(BaseModel):
    id: UUID
    provider: str
    label: str
    mode: str                          # test | live
    status: str                        # draft | enabled | disabled
    is_preferred: bool
    is_demo: bool
    connection: str                    # connected | credentials_ok | not_validated | invalid
    credentials_status: str
    webhook_status: str
    last_check_at: datetime | None
    last_check_ok: bool | None
    ready_for_use: bool
    ready_for_live: bool
    live_blockers: list[str]
    currencies: list[str]


class GatewayToSetUp(BaseModel):
    provider: str
    label: str
    modes: list[str]
    credential_fields: list[str]


class LiveState(BaseModel):
    deployment_allows_live: bool       # the LIVE_PAYMENTS_ENABLED kill switch (default on)
    active: bool
    activated_at: datetime | None
    activated_by_user_id: UUID | None
    config_id: UUID | None
    can_activate: bool                 # a live gateway exists that passes every requirement
    activation_blockers: list[str]     # for the best live candidate (empty when can_activate)


class PaymentOverview(BaseModel):
    payment_mode: str                  # demo | test | live | none
    payments_open: bool                # customers can start a payment right now
    message: str
    preferred: GatewaySummary | None
    live: LiveState
    gateways: list[GatewaySummary]
    to_set_up: list[GatewayToSetUp]
    warnings: list[str]


class DemoEnable(BaseModel):
    model_config = {"extra": "forbid"}

    confirm_no_real_money: bool = Field(description="Must be true: you confirm that demo payments move no real money and are for testing only")


class LiveActivate(BaseModel):
    model_config = {"extra": "forbid"}

    config_id: UUID = Field(description="The LIVE gateway configuration to take payments with")
    confirmation: str = Field(description='Type exactly: ACTIVATE LIVE PAYMENTS')
    note: str | None = Field(default=None, max_length=500)


class LiveDeactivate(BaseModel):
    model_config = {"extra": "forbid"}

    reason: str = Field(min_length=3, max_length=500)


class CheckItem(BaseModel):
    check: str
    status: str                        # pass | fail | warn | skipped
    required: bool
    message: str


class CheckResult(BaseModel):
    ok: bool                           # every REQUIRED check passed
    checked_at: datetime
    checks: list[CheckItem]
