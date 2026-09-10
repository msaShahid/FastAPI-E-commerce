import math
import uuid

import strawberry

from app.graphql.types.user import UserPage, UserType


@strawberry.type
class UserQuery:

    @strawberry.field
    async def me(
        self,
        info: strawberry.Info,
    ) -> UserType:
        """
        Return the currently authenticated user.
        """
        current_user = info.context.current_user

        return UserType.from_model(current_user)

    @strawberry.field
    async def user(
        self,
        info: strawberry.Info,
        id: uuid.UUID,
    ) -> UserType:
        """
        Return a user by ID.
        Authorization is handled by UserService.
        """

        current_user = info.context.current_user
        service = info.context.user_service

        user = await service.get_user(
            target_user_id=id,
            current_user=current_user,
        )

        return UserType.from_model(user)

    @strawberry.field
    async def users(
        self,
        info: strawberry.Info,
        page: int = 1,
        page_size: int = 10,
    ) -> UserPage:
        """
        List users with pagination.

        Only administrators are allowed to list users.
        Authorization is handled by UserService.list_users().
        """

        if page < 1:
            raise ValueError("page must be greater than or equal to 1")

        if page_size < 1:
            raise ValueError("page_size must be greater than or equal to 1")

        if page_size > 100:
            raise ValueError("page_size cannot exceed 100")

        current_user = info.context.current_user
        service = info.context.user_service

        offset = (page - 1) * page_size

        users, total = await service.list_users(
            current_user=current_user,
            offset=offset,
            limit=page_size,
        )

        total_pages = math.ceil(total / page_size) if total else 0

        return UserPage(
            items=[UserType.from_model(user) for user in users],
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
        )
