from uuid import UUID

from app.modules.addresses.exceptions.address_exceptions import AddressNotFoundError
from app.modules.addresses.models.address import Address
from app.modules.addresses.repositories.address_repository import AddressRepository


class AddressService:
    def __init__(self, repository: AddressRepository) -> None:
        self.repository = repository

    async def _get_owned(self, *, address_id: int, user_id: UUID) -> Address:

        address = await self.repository.get_by_id(address_id)
        if address is None or address.user_id != user_id:
            raise AddressNotFoundError(address_id)
        return address

    async def list_addresses(self, user_id: UUID) -> list[Address]:
        return await self.repository.list_for_user(user_id)

    async def get_address(self, *, address_id: int, user_id: UUID) -> Address:
        return await self._get_owned(address_id=address_id, user_id=user_id)

    async def create_address(
        self,
        *,
        user_id: UUID,
        recipient_name: str,
        line1: str,
        line2: str | None,
        city: str,
        state: str,
        postal_code: str,
        country: str,
        is_default: bool,
    ) -> Address:
        existing = await self.repository.list_for_user(user_id)

        make_default = is_default or not existing

        if make_default and existing:
            await self.repository.clear_default_for_user(user_id)

        return await self.repository.create(
            user_id=user_id,
            recipient_name=recipient_name,
            line1=line1,
            line2=line2,
            city=city,
            state=state,
            postal_code=postal_code,
            country=country,
            is_default=make_default,
        )

    async def update_address(
        self,
        *,
        address_id: int,
        user_id: UUID,
        recipient_name: str | None,
        line1: str | None,
        line2: str | None,
        city: str | None,
        state: str | None,
        postal_code: str | None,
        country: str | None,
        is_default: bool | None,
    ) -> Address:
        address = await self._get_owned(address_id=address_id, user_id=user_id)

        updates: dict = {}
        if recipient_name is not None:
            updates["recipient_name"] = recipient_name
        if line1 is not None:
            updates["line1"] = line1
        if line2 is not None:
            updates["line2"] = line2
        if city is not None:
            updates["city"] = city
        if state is not None:
            updates["state"] = state
        if postal_code is not None:
            updates["postal_code"] = postal_code
        if country is not None:
            updates["country"] = country

        if is_default is True and not address.is_default:
            await self.repository.clear_default_for_user(user_id)
            updates["is_default"] = True
        elif is_default is False:

            if (
                address.is_default
                and len(await self.repository.list_for_user(user_id)) > 1
            ):
                updates["is_default"] = False

        return await self.repository.update(address, **updates)

    async def delete_address(self, *, address_id: int, user_id: UUID) -> None:
        address = await self._get_owned(address_id=address_id, user_id=user_id)
        was_default = address.is_default
        await self.repository.delete(address)

        if was_default:
            remaining = await self.repository.list_for_user(user_id)
            if remaining:
                await self.repository.update(remaining[0], is_default=True)
