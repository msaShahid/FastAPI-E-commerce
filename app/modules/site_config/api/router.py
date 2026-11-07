"""The storefront's configuration (navigation, announcement, home sections, feature flags). Administrators edit a DRAFT, publish it as a new
version, and can roll back to any earlier one. The public endpoint serves only the published version."""

from fastapi import APIRouter, Depends, Response

from app.modules.roles.dependencies.permission_deps import ContentEdit, ContentPublish
from app.modules.site_config.dependencies.site_config_deps import SiteConfigServiceDep
from app.modules.site_config.schemas.site_config import (
    AdminSiteConfig,
    PublicSiteConfig,
    PublishRequest,
    RollbackRequest,
    SiteConfigDocument,
    VersionPage,
    VersionRead,
)
from app.shared.pagination.schemas import PageParams

site_config_router = APIRouter(tags=["site-config"])


@site_config_router.get("/site-config", response_model=PublicSiteConfig)
async def get_site_config(service: SiteConfigServiceDep, response: Response) -> PublicSiteConfig:
    """Public: the published storefront configuration. Short-lived caching; the storefront is also told when it changes."""
    response.headers["Cache-Control"] = "public, max-age=30"
    return await service.public()


@site_config_router.get("/admin/site-config", response_model=AdminSiteConfig)
async def get_admin_site_config(_admin: ContentEdit, service: SiteConfigServiceDep) -> AdminSiteConfig:
    return await service.overview()


@site_config_router.put("/admin/site-config/draft", response_model=AdminSiteConfig)
async def save_draft(payload: SiteConfigDocument, admin: ContentEdit, service: SiteConfigServiceDep) -> AdminSiteConfig:
    """Saves the working draft (the whole document). Validated against the limits the storefront can render. The storefront does not see it yet."""
    return await service.save_draft(admin, payload)


@site_config_router.delete("/admin/site-config/draft", response_model=AdminSiteConfig)
async def discard_draft(admin: ContentEdit, service: SiteConfigServiceDep) -> AdminSiteConfig:
    return await service.discard_draft(admin)


@site_config_router.post("/admin/site-config/publish", response_model=VersionRead)
async def publish(payload: PublishRequest, admin: ContentPublish, service: SiteConfigServiceDep) -> VersionRead:
    """Publishes the draft as a new version. The storefront is told right after the commit."""
    return await service.publish(admin, note=payload.note, expected_published_version=payload.expected_published_version)


@site_config_router.get("/admin/site-config/versions", response_model=VersionPage)
async def list_versions(_admin: ContentEdit, service: SiteConfigServiceDep, params: PageParams = Depends()) -> VersionPage:
    return await service.versions(offset=params.offset, limit=params.page_size)


@site_config_router.get("/admin/site-config/versions/{version}", response_model=VersionRead)
async def get_version(version: int, _admin: ContentEdit, service: SiteConfigServiceDep) -> VersionRead:
    return await service.version(version)


@site_config_router.post("/admin/site-config/rollback/{version}", response_model=VersionRead)
async def rollback(version: int, payload: RollbackRequest, admin: ContentPublish, service: SiteConfigServiceDep) -> VersionRead:
    """Publishes a copy of an earlier version as a NEW version (history is never rewritten)."""
    return await service.rollback(admin, version, note=payload.note)
