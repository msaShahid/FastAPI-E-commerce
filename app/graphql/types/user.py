import uuid

import strawberry

from app.modules.auth.models.user import User
from app.shared.enums.roles import UserRole


@strawberry.type
class UserType:
    id: uuid.UUID
    username: str
    email: str
    role: str
    is_active: bool

    @classmethod
    def from_model(cls, user: User) -> "UserType":
        role = (
            user.role.value
            if isinstance(user.role, UserRole)
            else str(user.role)
        )

        return cls(
            id=user.id,
            username=user.username,
            email=user.email,
            role=role,
            is_active=user.is_active,
        )