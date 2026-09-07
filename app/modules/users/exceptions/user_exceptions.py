from app.core.exceptions import ForbiddenError, NotFoundError


class UserNotFoundError(NotFoundError):
    def __init__(self, user_id) -> None:
        super().__init__("User not found")


class ForbiddenActionError(ForbiddenError):
    def __init__(self, message: str) -> None:
        super().__init__(message)