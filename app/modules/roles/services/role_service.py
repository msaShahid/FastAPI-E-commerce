import uuid

from app.modules.audit.services.audit_service import AuditService
from app.modules.auth.models.user import User
from app.modules.roles.exceptions.role_exceptions import (
    RoleInUseError,
    RoleNameTakenError,
    RoleNotFoundError,
    StaffMemberNotFoundError,
    SystemRoleError,
)
from app.modules.roles.models.staff_role import StaffRole, StaffRoleAssignment
from app.modules.roles.permissions import NO_ACCESS_ROLE_ID, NO_ACCESS_ROLE_NAME, Permission
from app.modules.roles.repositories.role_repository import RoleRepository
from app.modules.roles.services.access_service import AccessService


def _sorted(permissions: set[Permission] | frozenset[Permission]) -> list[str]:
    return sorted(p.value for p in permissions)


class RoleService:
    def __init__(self, repository: RoleRepository, access: AccessService, audit: AuditService) -> None:
        self.repository = repository
        self.access = access
        self.audit = audit

    async def list_roles(self) -> list[tuple[StaffRole, int]]:
        await self.repository.ensure_system_roles()
        return await self.repository.list_roles()

    async def get_role(self, role_id: uuid.UUID, *, for_update: bool = False) -> StaffRole:
        role = await self.repository.get_role(role_id, for_update=for_update)
        if role is None:
            raise RoleNotFoundError()
        return role

    async def create_role(self, actor: User, *, name: str, description: str | None, permissions: set[Permission]) -> StaffRole:
        if await self.repository.name_taken(name):
            raise RoleNameTakenError()
        role = await self.repository.add(StaffRole(name=name, description=description, permissions=_sorted(permissions)))
        await self.audit.log(actor_user_id=actor.id, action="staff_role.created", target_type="staff_role", target_id=role.id,
                             details={"name": role.name, "permissions": role.permissions})
        return role

    async def update_role(
        self, actor: User, role_id: uuid.UUID, *, name: str | None, description: str | None, description_set: bool,
        permissions: set[Permission] | None,
    ) -> StaffRole:
        role = await self.get_role(role_id, for_update=True)
        if role.is_system:
            raise SystemRoleError()
        before = {"name": role.name, "permissions": list(role.permissions)}
        if name is not None and name != role.name:
            if await self.repository.name_taken(name, excluding=role.id):
                raise RoleNameTakenError()
            role.name = name
        if description_set:
            role.description = description
        if permissions is not None:
            if Permission.ROLES_MANAGE not in permissions:
                await self.access.assert_role_manager_remains(role_change=(role.id, frozenset(permissions)))
            role.permissions = _sorted(permissions)
        role = await self.repository.save(role)
        await self.audit.log(actor_user_id=actor.id, action="staff_role.updated", target_type="staff_role", target_id=role.id,
                             details={"before": before, "after": {"name": role.name, "permissions": list(role.permissions)}})
        return role

    async def delete_role(self, actor: User, role_id: uuid.UUID) -> None:
        role = await self.get_role(role_id, for_update=True)
        if role.is_system:
            raise SystemRoleError()
        members = await self.repository.count_assignments(role.id)
        if members:
            raise RoleInUseError(members)
        await self.repository.delete(role)
        await self.audit.log(actor_user_id=actor.id, action="staff_role.deleted", target_type="staff_role", target_id=role_id,
                             details={"name": role.name})

    async def list_staff(self, *, offset: int, limit: int) -> tuple[list[tuple[User, StaffRoleAssignment | None]], int]:
        return await self.repository.list_staff(offset=offset, limit=limit)

    async def assign_role(self, actor: User, user_id: uuid.UUID, role_id: uuid.UUID) -> tuple[User, StaffRoleAssignment]:
        await self.repository.ensure_system_roles()
        role = await self.get_role(role_id)
        target = await self.repository.get_active_admin(user_id)
        if target is None:
            raise StaffMemberNotFoundError()
        previous = await self.repository.get_assignment(user_id)
        previous_name = previous.role.name if previous is not None else None
        await self.access.assert_role_manager_remains(user_changes={user_id: role})
        assignment = await self.repository.set_assignment(user_id=user_id, role_id=role.id, actor_id=actor.id)
        await self.audit.log(actor_user_id=actor.id, action="staff.role_assigned", target_type="user", target_id=user_id,
                             details={"from": previous_name, "to": role.name})
        return target, assignment

    async def start_with_no_access(self, actor: User, user_id: uuid.UUID) -> None:
        """Someone just promoted to administrator holds nothing until a role is assigned to them."""
        await self.repository.ensure_system_roles()
        await self.repository.set_assignment(user_id=user_id, role_id=NO_ACCESS_ROLE_ID, actor_id=actor.id)
        await self.audit.log(actor_user_id=actor.id, action="staff.role_assigned", target_type="user", target_id=user_id,
                             details={"from": None, "to": NO_ACCESS_ROLE_NAME})
