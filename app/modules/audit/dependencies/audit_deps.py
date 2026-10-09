from typing import Annotated

from fastapi import Depends

from app.core.database import DbSession
from app.modules.audit.repositories.audit_repository import AuditRepository
from app.modules.audit.services.audit_service import AuditService


def get_audit_repository(db: DbSession) -> AuditRepository:
    return AuditRepository(db)


def get_audit_service(
    repository: Annotated[AuditRepository, Depends(get_audit_repository)],
) -> AuditService:
    return AuditService(repository)


AuditServiceDep = Annotated[AuditService, Depends(get_audit_service)]
