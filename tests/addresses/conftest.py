from uuid import UUID

import pytest

from app.modules.addresses.models.address import Address


class FakeAddressRepository:
    def __init__(self) -> None:
        self.addresses: dict[int, Address] = {}
        self._next_id = 1

    async def get_by_id(self, address_id: int) -> Address | None:
        return self.addresses.get(address_id)

    async def list_for_user(self, user_id: UUID) -> list[Address]:
        results = [a for a in self.addresses.values() if a.user_id == user_id]
        results.sort(key=lambda a: (not a.is_default, a.id), reverse=False)
        return results

    async def get_default_for_user(self, user_id: UUID) -> Address | None:
        return next(
            (
                a
                for a in self.addresses.values()
                if a.user_id == user_id and a.is_default
            ),
            None,
        )

    async def create(self, **fields) -> Address:
        address = Address(id=self._next_id, **fields)
        self.addresses[self._next_id] = address
        self._next_id += 1
        return address

    async def update(self, address: Address, **fields) -> Address:
        for key, value in fields.items():
            setattr(address, key, value)
        return address

    async def delete(self, address: Address) -> None:
        self.addresses.pop(address.id, None)

    async def clear_default_for_user(self, user_id: UUID) -> None:
        for address in self.addresses.values():
            if address.user_id == user_id:
                address.is_default = False


@pytest.fixture
def fake_address_repository() -> FakeAddressRepository:
    return FakeAddressRepository()