from typing import Annotated

from fastapi import Depends

from app.core.database import DbSession
from app.modules.addresses.repositories.address_repository import AddressRepository
from app.modules.addresses.services.address_service import AddressService


def get_address_repository(db: DbSession) -> AddressRepository:
    return AddressRepository(db)


def get_address_service(
    repository: Annotated[AddressRepository, Depends(get_address_repository)],
) -> AddressService:
    return AddressService(repository)


AddressServiceDep = Annotated[AddressService, Depends(get_address_service)]
