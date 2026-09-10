import uuid

import strawberry

from app.graphql.inputs.user import UpdateUserInput
from app.graphql.types.user import UserType


@strawberry.type
class UserMutation:

    @strawberry.mutation
    async def update_user(
        self,
        info: strawberry.Info,
        input: UpdateUserInput,
    ) -> UserType:
        """
        Update a user.
        Authorization and business rules are handled by UserService.update_user().
        """

        current_user = info.context.current_user
        service = info.context.user_service

        user = await service.update_user(
            target_user_id=input.user_id,
            current_user=current_user,
            username=input.username,
            email=input.email,
            role=input.role,
        )

        await info.context.db.commit()

        return UserType.from_model(user)

    @strawberry.mutation
    async def deactivate_user(
        self,
        info: strawberry.Info,
        user_id: uuid.UUID,
    ) -> bool:
        """
        Deactivate a user.
        Authorization is handled by UserService.deactivate_user().
        """

        current_user = info.context.current_user
        service = info.context.user_service

        user = await service.deactivate_user(
            target_user_id=user_id,
            current_user=current_user,
        )

        await info.context.db.commit()

        return not user.is_active
