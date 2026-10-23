from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class _Strict(BaseModel):
    # Unknown fields are REJECTED: a request cannot smuggle in a store id or any other selector.
    model_config = ConfigDict(extra="forbid")


class CredentialsInput(_Strict):
    """Write-only. Stripe: secret_key. Razorpay: key_id and key_secret. Never returned by any endpoint."""

    secret_key: SecretStr | None = None
    key_id: SecretStr | None = None
    key_secret: SecretStr | None = None


class ProviderConfigCreate(_Strict):
    provider: str = Field(min_length=2, max_length=30)
    mode: Literal["test", "live"]
    credentials: CredentialsInput
    webhook_secret: SecretStr | None = None
    currencies: list[str] | None = None


class CredentialsRotate(_Strict):
    credentials: CredentialsInput | None = None
    webhook_secret: SecretStr | None = None


class CurrenciesUpdate(_Strict):
    currencies: list[str] = Field(max_length=30)


class AttestationCreate(_Strict):
    kind: Literal["sandbox_verified", "production_review"]
    reference: str = Field(min_length=3, max_length=2000, description="Where the verification/review record can be found")


class Capabilities(BaseModel):
    currencies: list[str]
    supports_cancel: bool
    supports_full_refund: bool
    dispute_events: bool
    refund_needs_provider_reference: bool
    verification: str


class CatalogEntry(BaseModel):
    provider: str
    label: str
    modes: list[str]
    credential_fields: list[str]
    capabilities: Capabilities
    webhook_events: list[str]


class CredentialStatus(BaseModel):
    configured: bool
    hint: str | None
    status: str
    validated_at: datetime | None
    public_identifier: str | None


class WebhookStatus(BaseModel):
    secret_configured: bool
    hint: str | None
    previous_secret_active: bool
    status: str
    verified_at: datetime | None
    url: str
    events: list[str]


class Readiness(BaseModel):
    ready_for_use: bool
    ready_for_live: bool
    blockers: list[str]
    live_blockers: list[str]


class ProviderConfigRead(BaseModel):
    """Sanitized: no secret, ciphertext or decryptable value is ever included."""

    id: UUID
    provider: str
    label: str
    mode: str
    status: str
    is_preferred: bool
    currencies: list[str]
    credentials: CredentialStatus
    webhook: WebhookStatus
    capabilities: Capabilities
    readiness: Readiness
    sandbox_verified_at: datetime | None
    production_review_at: datetime | None
    version: int
    created_by_user_id: UUID | None
    updated_by_user_id: UUID | None
    created_at: datetime
    updated_at: datetime


class ValidationResult(BaseModel):
    valid: bool
    reason: str | None
    detected_mode: str | None
    config: ProviderConfigRead
