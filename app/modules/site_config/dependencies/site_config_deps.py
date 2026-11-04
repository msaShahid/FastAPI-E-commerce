from typing import Annotated

from fastapi import Depends

from app.core.database import DbSession
from app.modules.audit.dependencies.audit_deps import AuditServiceDep
from app.modules.site_config.repositories.site_config_repository import SiteConfigRepository
from app.modules.site_config.services.site_config_service import SiteConfigService


def get_site_config_service(db: DbSession, audit: AuditServiceDep) -> SiteConfigService:
    return SiteConfigService(SiteConfigRepository(db), audit)


SiteConfigServiceDep = Annotated[SiteConfigService, Depends(get_site_config_service)]
