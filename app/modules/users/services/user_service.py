import uuid

from app.modules.auth.models.user import User
from app.modules.users.exceptions.user_exceptions import (
    ForbiddenActionError,
    LastAdminError,
    UserNotFoundError,
)
from app.modules.users.repositories.user_repository import UserRepository
from app.shared.enums.roles import UserRole


class UserService:

    def __init__(self, repository: UserRepository) -> None:
        self.repository = repository

    async def _assert_not_last_admin(self, user: User) -> None:
        """Refuses to remove the last active administrator (by demotion or deactivation), including under concurrency."""
        if user.role != UserRole.ADMIN or not user.is_active:
            return
        if not [a for a in await self.repository.lock_active_admins() if a.id != user.id]:
            raise LastAdminError()

    async def get_user(self, *, target_user_id: uuid.UUID, current_user: User) -> User:
       
        if current_user.role != UserRole.ADMIN and current_user.id != target_user_id:
            raise ForbiddenActionError("You can only view your own profile")

        user = await self.repository.get_by_id(target_user_id)
        if user is None:
            raise UserNotFoundError(target_user_id)
        return user

    async def list_users(
        self, *, current_user: User, offset: int, limit: int
    ) -> tuple[list[User], int]:

        if current_user.role != UserRole.ADMIN:
            raise ForbiddenActionError("Only administrators can list all users")

        return await self.repository.list_paginated(offset=offset, limit=limit)

    async def update_user(
        self,
        *,
        target_user_id: uuid.UUID,
        current_user: User,
        username: str | None,
        email: str | None,
        role: UserRole | None,
    ) -> User:
        if current_user.role != UserRole.ADMIN and current_user.id != target_user_id:
            raise ForbiddenActionError("You can only update your own profile")

        if role is not None and current_user.role != UserRole.ADMIN:
            raise ForbiddenActionError("Only administrators can change roles")

        user = await self.repository.get_by_id(target_user_id)
        if user is None:
            raise UserNotFoundError(target_user_id)
        if role is not None and role != UserRole.ADMIN:
            await self._assert_not_last_admin(user)

        updates = {}
        if username is not None:
            updates["username"] = username
        if email is not None and email != user.email:
            if current_user.id == target_user_id:
                # Your own address changes only through the confirmed flow (current password, link to the NEW address, notice to
                # the old one): a stolen session must not be able to quietly redirect the account's mail.
                raise ForbiddenActionError("Change your email address with POST /auth/email/change")
            updates["email"] = email  # an administrator correcting someone else's record: it is unverified until they confirm it
            updates["email_verified_at"] = None
        if role is not None:
            updates["role"] = role

        return await self.repository.update(user, **updates)

    async def deactivate_user(
        self, *, target_user_id: uuid.UUID, current_user: User
    ) -> User:

        if current_user.role != UserRole.ADMIN and current_user.id != target_user_id:
            raise ForbiddenActionError("You can only deactivate your own account")

        user = await self.repository.get_by_id(target_user_id)
        if user is None:
            raise UserNotFoundError(target_user_id)
        await self._assert_not_last_admin(user)

        return await self.repository.deactivate(user)
