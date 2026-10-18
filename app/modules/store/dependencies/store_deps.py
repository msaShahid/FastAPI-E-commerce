from typing import Annotated

from fastapi import Depends

from app.core.database import DbSession
from app.modules.audit.dependencies.audit_deps import AuditServiceDep
from app.modules.store.repositories.store_repository import StoreRepository
from app.modules.store.services.store_service import StoreService


def get_store_repository(db: DbSession) -> StoreRepository:
    return StoreRepository(db)


def get_store_service(
    repository: Annotated[StoreRepository, Depends(get_store_repository)], audit: AuditServiceDep
) -> StoreService:
    return StoreService(repository, audit)


StoreServiceDep = Annotated[StoreService, Depends(get_store_service)]
