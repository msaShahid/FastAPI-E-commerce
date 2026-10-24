from app.core.exceptions import ConflictError, ForbiddenError, NotFoundError


class UserNotFoundError(NotFoundError):
    def __init__(self, user_id) -> None:
        super().__init__("User not found")


class LastAdminError(ConflictError):
    code = "last_admin"

    def __init__(self) -> None:
        super().__init__(
            "This is the last active administrator: demoting or deactivating it would leave nobody able to administer the store. "
            "Promote or create another administrator first."
        )


class ForbiddenActionError(ForbiddenError):
    def __init__(self, message: str) -> None:
        super().__init__(message)