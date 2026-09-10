import uuid

import strawberry

from app.graphql.types.user import UserType


@strawberry.type
class UserQuery:

    @strawberry.field
    async def me( self, info: strawberry.Info, ) -> UserType:
        """
        Return the currently authenticated user.
        """
        current_user = info.context.current_user

        return UserType.from_model(current_user)

    @strawberry.field
    async def user( self,  info: strawberry.Info,  id: uuid.UUID, ) -> UserType:
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
