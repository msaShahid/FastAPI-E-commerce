from uuid import UUID

from app.modules.addresses.exceptions.address_exceptions import (
    AddressLimitReachedError,
    AddressNotFoundError,
)
from app.modules.addresses.models.address import Address
from app.modules.addresses.repositories.address_repository import AddressRepository
from app.shared.limits import MAX_ADDRESSES_PER_USER


class AddressService:
    def __init__(self, repository: AddressRepository) -> None:
        self.repository = repository

    async def _get_owned(self, *, address_id: int, user_id: UUID) -> Address:
        """
        Looked up by id, then ownership-checked -- a 404, not a 403, for
        an address that exists but belongs to someone else, so this
        never confirms another user's address id exists (same pattern
        as OrderService.get_order's ownership check).
        """
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

        # The list endpoint is not paginated, so the collection is capped
        # instead. (Two simultaneous creates can overshoot by one or two; the
        # point is to keep it bounded, not to count exactly.)
        if len(existing) >= MAX_ADDRESSES_PER_USER:
            raise AddressLimitReachedError(MAX_ADDRESSES_PER_USER)

        # The user's very first address is always the default, whatever
        # they passed in -- there's never a moment with a saved address
        # but no default to fall back to at checkout.
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
            # Refuse to leave the user with zero default addresses --
            # unsetting the only default has no replacement to fall back
            # to, so checkout's "use my default address" would silently
            # stop working. Turning another address on is how you
            # actually change the default (that path clears this one).
            if address.is_default and len(await self.repository.list_for_user(user_id)) > 1:
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
