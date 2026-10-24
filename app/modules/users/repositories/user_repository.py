import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models.user import User
from app.shared.enums.roles import UserRole


class UserRepository:

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return await self.db.get(User, user_id)

    async def list_paginated(
        self, *, offset: int, limit: int
    ) -> tuple[list[User], int]:

        total_result = await self.db.execute(select(func.count()).select_from(User))
        total = total_result.scalar_one()

        items_result = await self.db.execute(
            select(User).order_by(User.created_at.desc()).offset(offset).limit(limit)
        )
        items = list(items_result.scalars().all())

        return items, total

    async def lock_active_admins(self) -> list[User]:
        """Row-locks every ACTIVE administrator (in id order, so two requests cannot deadlock). Two admins demoting each other at
        the same moment therefore serialise: the second sees the first's committed result."""
        result = await self.db.execute(
            select(User).where(User.role == UserRole.ADMIN, User.is_active.is_(True)).order_by(User.id).with_for_update()
        )
        return list(result.scalars().all())

    async def update(self, user: User, **fields) -> User:
        for key, value in fields.items():
            setattr(user, key, value)
        await self.db.flush()
        return user

    async def deactivate(self, user: User) -> User:

        user.is_active = False
        await self.db.flush()
        return user
