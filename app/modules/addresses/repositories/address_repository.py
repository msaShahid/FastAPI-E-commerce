from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.addresses.models.address import Address


class AddressRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, address_id: int) -> Address | None:
        return await self.db.get(Address, address_id)

    async def list_for_user(self, user_id: UUID) -> list[Address]:
        result = await self.db.execute(
            select(Address)
            .where(Address.user_id == user_id)
            .order_by(Address.is_default.desc(), Address.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_default_for_user(self, user_id: UUID) -> Address | None:
        result = await self.db.execute(
            select(Address).where(
                Address.user_id == user_id, Address.is_default.is_(True)
            )
        )
        return result.scalar_one_or_none()

    async def create(self, **fields) -> Address:
        address = Address(**fields)
        self.db.add(address)
        await self.db.flush()
        return address

    async def update(self, address: Address, **fields) -> Address:
        for key, value in fields.items():
            setattr(address, key, value)
        await self.db.flush()
        return address

    async def delete(self, address: Address) -> None:
        await self.db.delete(address)
        await self.db.flush()

    async def clear_default_for_user(self, user_id: UUID) -> None:

        addresses = await self.list_for_user(user_id)
        for address in addresses:
            if address.is_default:
                address.is_default = False
        await self.db.flush()
