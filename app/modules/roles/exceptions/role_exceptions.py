from app.core.exceptions import ConflictError, ForbiddenError, InvalidStateError, NotFoundError


class PermissionDeniedError(ForbiddenError):
    code = "permission_denied"

    def __init__(self, permission: str) -> None:
        super().__init__("Your role does not allow this action")
        self.extra = {"permission": permission}


class RoleNotFoundError(NotFoundError):
    code = "role_not_found"

    def __init__(self) -> None:
        super().__init__("That role does not exist")


class RoleNameTakenError(ConflictError):
    code = "role_name_taken"

    def __init__(self) -> None:
        super().__init__("A role with that name already exists")


class SystemRoleError(InvalidStateError):
    code = "role_is_system"

    def __init__(self) -> None:
        super().__init__("System roles (Owner, No access) cannot be changed or deleted")


class RoleInUseError(ConflictError):
    code = "role_in_use"

    def __init__(self, members: int) -> None:
        super().__init__("Move every staff member off this role before deleting it")
        self.extra = {"members": members}


class StaffMemberNotFoundError(NotFoundError):
    code = "staff_member_not_found"

    def __init__(self) -> None:
        super().__init__("No active administrator with that id")


class LastRoleManagerError(ConflictError):
    code = "last_role_manager"

    def __init__(self) -> None:
        super().__init__("At least one active administrator must keep the permission to manage roles")
