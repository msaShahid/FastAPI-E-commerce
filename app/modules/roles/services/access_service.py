import uuid

from app.modules.auth.models.user import User
from app.modules.roles.exceptions.role_exceptions import LastRoleManagerError, PermissionDeniedError
from app.modules.roles.models.staff_role import StaffRole
from app.modules.roles.permissions import ALL_PERMISSIONS, OWNER_ROLE_ID, Permission, effective
from app.modules.roles.repositories.role_repository import RoleRepository
from app.shared.enums.roles import UserRole

# Marks a user who will no longer be an active administrator after the change being checked.
LEAVES_STAFF = object()


def role_permissions(role: StaffRole | None) -> frozenset[Permission]:
    """No role (legacy administrator) or the Owner role: everything. Otherwise the stored, still-known permissions and what
    they include (the No access system role stores none)."""
    if role is None or role.id == OWNER_ROLE_ID:
        return ALL_PERMISSIONS
    known = {p.value for p in Permission}
    return effective({Permission(p) for p in role.permissions if p in known})


class AccessService:
    """Answers what a user may do. One instance per request (FastAPI caches the dependency), so lookups are cached too."""

    def __init__(self, repository: RoleRepository) -> None:
        self.repository = repository
        self._cache: dict[uuid.UUID, tuple[frozenset[Permission], StaffRole | None]] = {}

    async def resolve(self, user: User) -> tuple[frozenset[Permission], StaffRole | None]:
        if user.role != UserRole.ADMIN or not user.is_active:
            return frozenset(), None
        if user.id not in self._cache:
            assignment = await self.repository.get_assignment(user.id)
            role = assignment.role if assignment is not None else None
            self._cache[user.id] = (role_permissions(role), role)
        return self._cache[user.id]

    async def has(self, user: User, permission: Permission) -> bool:
        permissions, _ = await self.resolve(user)
        return permission in permissions

    async def require(self, user: User, permission: Permission) -> None:
        if not await self.has(user, permission):
            raise PermissionDeniedError(permission.value)

    async def assert_role_manager_remains(
        self,
        *,
        user_changes: dict[uuid.UUID, object] | None = None,
        role_change: tuple[uuid.UUID, frozenset[Permission]] | None = None,
    ) -> None:
        """Refuses a change that would leave no active administrator able to manage roles (locks the admin rows first).

        user_changes maps a user id to their new StaffRole, or LEAVES_STAFF if they stop being an active administrator.
        role_change is (role id, its new permission set) for an edit to a role's permissions.
        """
        user_changes = user_changes or {}
        for user, assignment in await self.repository.lock_active_admins():
            if user.id in user_changes:
                new = user_changes[user.id]
                if new is LEAVES_STAFF:
                    continue
                role = new if isinstance(new, StaffRole) else None
            else:
                role = assignment.role if assignment is not None else None
            if role is not None and role_change is not None and role.id == role_change[0] and not role.is_system:
                permissions = effective(role_change[1])
            else:
                permissions = role_permissions(role)
            if Permission.ROLES_MANAGE in permissions:
                return
        raise LastRoleManagerError()
