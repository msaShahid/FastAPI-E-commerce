from typing import Annotated

from fastapi import Depends

from app.core.database import DbSession
from app.modules.audit.dependencies.audit_deps import AuditServiceDep
from app.modules.tax.repositories.tax_repository import TaxRepository
from app.modules.tax.services.tax_service import TaxService


def get_tax_service(db: DbSession, audit: AuditServiceDep) -> TaxService:
    return TaxService(TaxRepository(db), audit)


TaxServiceDep = Annotated[TaxService, Depends(get_tax_service)]
