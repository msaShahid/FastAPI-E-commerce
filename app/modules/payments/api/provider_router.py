"""
Admin API for payment provider configuration. Administrator only. There is no store/tenant parameter anywhere: a NovaCart
deployment serves exactly one store, so these endpoints can only ever see and change THIS store's configuration. Secrets are
write-only: no endpoint returns a credential, ciphertext or webhook secret (only a non-secret hint and statuses).
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.core.database import DbSession
from app.modules.audit.dependencies.audit_deps import AuditServiceDep
from app.modules.auth.dependencies.auth import AdminUser
from app.modules.payments.repositories.provider_config_repository import ProviderConfigRepository
from app.modules.payments.schemas.provider_config import (
    AttestationCreate,
    CatalogEntry,
    CredentialsRotate,
    CurrenciesUpdate,
    ProviderConfigCreate,
    ProviderConfigRead,
    ValidationResult,
    WebhookStatus,
)
from app.modules.payments.services.provider_config_service import ProviderConfigService

provider_router = APIRouter(prefix="/admin/payment-providers", tags=["payment-providers"])


def get_provider_config_service(db: DbSession, audit: AuditServiceDep) -> ProviderConfigService:
    return ProviderConfigService(ProviderConfigRepository(db), audit)


ServiceDep = Annotated[ProviderConfigService, Depends(get_provider_config_service)]


@provider_router.get("/catalog", response_model=list[CatalogEntry])
async def provider_catalog(_admin: AdminUser, service: ServiceDep):
    """The providers this platform supports, their credential fields and what each is VERIFIED to support."""
    return service.catalog()


@provider_router.get("", response_model=list[ProviderConfigRead])
async def list_provider_configs(_admin: AdminUser, service: ServiceDep):
    return await service.list_configs()


@provider_router.post("", response_model=ProviderConfigRead, status_code=status.HTTP_201_CREATED)
async def create_provider_config(payload: ProviderConfigCreate, admin: AdminUser, service: ServiceDep):
    """Stores credentials ENCRYPTED. The new configuration is a draft: it enables nothing and selects nothing."""
    return await service.create(
        admin, provider=payload.provider, mode=payload.mode, credentials=payload.credentials,
        webhook_secret=payload.webhook_secret, currencies=payload.currencies,
    )


@provider_router.get("/{config_id}", response_model=ProviderConfigRead)
async def get_provider_config(config_id: UUID, _admin: AdminUser, service: ServiceDep):
    return await service.get_view(config_id)


@provider_router.put("/{config_id}/credentials", response_model=ProviderConfigRead)
async def rotate_credentials(config_id: UUID, payload: CredentialsRotate, admin: AdminUser, service: ServiceDep):
    """Rotates credentials and/or the webhook secret (write-only). New payments on this configuration PAUSE until it is validated
    and enabled again; existing payments keep reconciling. The previous webhook secret keeps verifying until retired."""
    return await service.rotate(admin, config_id, credentials=payload.credentials, webhook_secret=payload.webhook_secret)


@provider_router.post("/{config_id}/validate", response_model=ValidationResult)
async def validate_credentials(config_id: UUID, admin: AdminUser, service: ServiceDep):
    """Checks the stored credentials against the provider with a harmless authenticated call. Returns a safe reason code."""
    return await service.validate(admin, config_id)


@provider_router.put("/{config_id}/currencies", response_model=ProviderConfigRead)
async def set_provider_currencies(config_id: UUID, payload: CurrenciesUpdate, admin: AdminUser, service: ServiceDep):
    return await service.set_currencies(admin, config_id, payload.currencies)


@provider_router.post("/{config_id}/enable", response_model=ProviderConfigRead)
async def enable_provider(config_id: UUID, admin: AdminUser, service: ServiceDep):
    """Refused (409 provider_not_ready) with the list of blockers unless every readiness condition holds. Live additionally
    needs LIVE_PAYMENTS_ENABLED, a verified webhook, and recorded sandbox-verification and production-review attestations."""
    return await service.enable(admin, config_id)


@provider_router.post("/{config_id}/disable", response_model=ProviderConfigRead)
async def disable_provider(config_id: UUID, admin: AdminUser, service: ServiceDep):
    """Blocks NEW payments only. Existing payments, refunds and webhooks keep working; nothing is deleted."""
    return await service.disable(admin, config_id)


@provider_router.post("/{config_id}/prefer", response_model=ProviderConfigRead)
async def prefer_provider(config_id: UUID, admin: AdminUser, service: ServiceDep):
    """Makes this the provider for NEW payments. Existing orders, payments and refunds are not changed."""
    return await service.prefer(admin, config_id)


@provider_router.post("/{config_id}/attestations", response_model=ProviderConfigRead)
async def record_attestation(config_id: UUID, payload: AttestationCreate, admin: AdminUser, service: ServiceDep):
    """Records that sandbox verification / a production review was done (with where its record is). NovaCart cannot verify this
    itself: the attestation is an auditable statement by the administrator, required before a LIVE configuration can be enabled."""
    return await service.attest(admin, config_id, kind=payload.kind, reference=payload.reference)


@provider_router.post("/{config_id}/webhook-secret/retire-previous", response_model=ProviderConfigRead)
async def retire_previous_webhook_secret(config_id: UUID, admin: AdminUser, service: ServiceDep):
    return await service.retire_previous_webhook_secret(admin, config_id)


@provider_router.get("/{config_id}/webhook", response_model=WebhookStatus)
async def webhook_configuration(config_id: UUID, _admin: AdminUser, service: ServiceDep):
    """The URL to register at the provider, the events to subscribe to, and whether a correctly signed event has been received."""
    return (await service.get_view(config_id))["webhook"]


@provider_router.get("/{config_id}/readiness")
async def provider_readiness(config_id: UUID, _admin: AdminUser, service: ServiceDep):
    view = await service.get_view(config_id)
    return {"config_id": view["id"], "provider": view["provider"], "mode": view["mode"], "status": view["status"],
            **view["readiness"], "verification_note": view["capabilities"]["verification"]}
