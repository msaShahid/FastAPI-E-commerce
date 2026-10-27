"""Admin API for the email service. Administrator only; credentials are write-only (no endpoint returns one). There is no store
parameter: a deployment serves one store."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Request, status

from app.core.database import DbSession
from app.core.rate_limit import limiter
from app.modules.audit.dependencies.audit_deps import AuditServiceDep
from app.modules.auth.dependencies.auth import AdminUser
from app.modules.email_settings.repositories.email_provider_repository import (
    EmailProviderRepository,
)
from app.modules.email_settings.schemas.email_settings import (
    EmailProviderSave,
    EmailSettingsRead,
    ProbeResult,
    ProviderCatalogEntry,
    ProviderConfigRead,
)
from app.modules.email_settings.services.email_settings_service import EmailSettingsService

email_settings_router = APIRouter(prefix="/admin/email-providers", tags=["email-providers"])

ProviderName = Annotated[Literal["google", "zoho", "mailtrap"], Path(description="The email service")]


def get_email_settings_service(db: DbSession, audit: AuditServiceDep) -> EmailSettingsService:
    return EmailSettingsService(EmailProviderRepository(db), audit)


ServiceDep = Annotated[EmailSettingsService, Depends(get_email_settings_service)]


@email_settings_router.get("/catalog", response_model=list[ProviderCatalogEntry])
async def email_catalog(_admin: AdminUser, service: ServiceDep):
    """The email services that can be chosen, the fields each needs and setup notes. Hosts are fixed presets."""
    return service.catalog()


@email_settings_router.get("", response_model=EmailSettingsRead)
async def read_email_settings(_admin: AdminUser, service: ServiceDep):
    """Which service is in use right now, and the state of each of the three (configured, last test, last send)."""
    return await service.read_settings()


@email_settings_router.post("/deactivate", response_model=EmailSettingsRead)
async def deactivate_email_provider(admin: AdminUser, service: ServiceDep):
    """Stop using any selected service; the server's environment settings (EMAIL_BACKEND) apply again."""
    return await service.deactivate(admin)


@email_settings_router.put("/{provider}", response_model=ProviderConfigRead)
async def save_email_provider(provider: ProviderName, payload: EmailProviderSave, admin: AdminUser, service: ServiceDep):
    """Create or update a service's settings. Credentials are encrypted. Editing the ACTIVE service's credentials is tested first
    and refused if the test fails."""
    return await service.save(provider, payload, admin)


@email_settings_router.post("/{provider}/test", response_model=ProbeResult)
@limiter.limit("10/minute")
async def test_email_provider(request: Request, provider: ProviderName, admin: AdminUser, service: ServiceDep):
    """Connect and sign in with the stored credentials. Sends nothing."""
    return await service.test(provider, admin)


@email_settings_router.post("/{provider}/test-email", response_model=ProbeResult)
@limiter.limit("5/minute")
async def send_test_email(request: Request, provider: ProviderName, admin: AdminUser, service: ServiceDep):
    """Send one test message to YOUR OWN account address through this service (it need not be the active one)."""
    return await service.send_test_email(provider, admin)


@email_settings_router.post("/{provider}/activate", response_model=ProviderConfigRead)
@limiter.limit("10/minute")
async def activate_email_provider(request: Request, provider: ProviderName, admin: AdminUser, service: ServiceDep):
    """Make this the service every email goes through. A live connection test must pass first. Replaces any other active service."""
    return await service.activate(provider, admin)


@email_settings_router.delete("/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_email_provider(provider: ProviderName, admin: AdminUser, service: ServiceDep) -> None:
    """Remove a service's stored settings. The active one cannot be deleted."""
    await service.delete(provider, admin)
