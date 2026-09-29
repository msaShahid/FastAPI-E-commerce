from fastapi import APIRouter, status

from app.modules.addresses.dependencies.address_deps import AddressServiceDep
from app.modules.addresses.schemas.address import (
    AddressCreate,
    AddressRead,
    AddressUpdate,
)
from app.modules.auth.dependencies.auth import CurrentUser

address_router = APIRouter(prefix="/addresses", tags=["addresses"])


@address_router.post(
    "", response_model=AddressRead, status_code=status.HTTP_201_CREATED
)
async def create_address(
    payload: AddressCreate, current_user: CurrentUser, service: AddressServiceDep
) -> AddressRead:
    address = await service.create_address(
        user_id=current_user.id,
        recipient_name=payload.recipient_name,
        line1=payload.line1,
        line2=payload.line2,
        city=payload.city,
        state=payload.state,
        postal_code=payload.postal_code,
        country=payload.country,
        is_default=payload.is_default,
    )
    return AddressRead.model_validate(address)


@address_router.get("", response_model=list[AddressRead])
async def list_addresses(
    current_user: CurrentUser, service: AddressServiceDep
) -> list[AddressRead]:
    addresses = await service.list_addresses(current_user.id)
    return [AddressRead.model_validate(a) for a in addresses]


@address_router.get("/{address_id}", response_model=AddressRead)
async def get_address(
    address_id: int, current_user: CurrentUser, service: AddressServiceDep
) -> AddressRead:
    address = await service.get_address(address_id=address_id, user_id=current_user.id)
    return AddressRead.model_validate(address)


@address_router.patch("/{address_id}", response_model=AddressRead)
async def update_address(
    address_id: int,
    payload: AddressUpdate,
    current_user: CurrentUser,
    service: AddressServiceDep,
) -> AddressRead:
    address = await service.update_address(
        address_id=address_id,
        user_id=current_user.id,
        recipient_name=payload.recipient_name,
        line1=payload.line1,
        line2=payload.line2,
        city=payload.city,
        state=payload.state,
        postal_code=payload.postal_code,
        country=payload.country,
        is_default=payload.is_default,
    )
    return AddressRead.model_validate(address)


@address_router.delete("/{address_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_address(
    address_id: int, current_user: CurrentUser, service: AddressServiceDep
) -> None:
    await service.delete_address(address_id=address_id, user_id=current_user.id)
