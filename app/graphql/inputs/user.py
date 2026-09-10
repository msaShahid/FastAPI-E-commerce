import uuid

import strawberry

from app.shared.enums.roles import UserRole


@strawberry.input
class UpdateUserInput:
    user_id: uuid.UUID
    username: str | None = None
    email: str | None = None
    role: UserRole | None = None