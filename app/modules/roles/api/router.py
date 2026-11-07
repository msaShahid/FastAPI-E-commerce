"""Staff roles and permissions (BE-P1): what the signed-in user may do, the permission list, roles, and who holds which role."""

import uuid

from fastapi import APIRouter, Depends, Response, status

from app.modules.auth.dependencies.auth import CurrentUser
from app.modules.auth.models.user import User
from app.modules.roles.dependencies.permission_deps import (
    AccessServiceDep,
    RoleServiceDep,
    RolesManage,
)
from app.modules.roles.models.staff_role import StaffRole, StaffRoleAssignment
from app.modules.roles.permissions import Permission, role_permissions_for_display
from app.modules.roles.schemas.role import (
    MyAccess,
    PermissionRead,
    RoleAssignRequest,
    RoleCreate,
    RoleRead,
    RoleSummary,
    RoleUpdate,
    StaffMemberRead,
    StaffPage,
)
from app.modules.roles.services.access_service import role_permissions
from app.shared.enums.roles import UserRole
from app.shared.pagination.schemas import PageParams

roles_router = APIRouter(tags=["roles"])


def _role(role: StaffRole, members: int = 0) -> RoleRead:
    return RoleRead(
        id=role.id, name=role.name, is_system=role.is_system, description=role.description,
        permissions=role_permissions_for_display(role.id, role.permissions),
        effective_permissions=sorted(role_permissions(role)), member_count=members,
        created_at=role.created_at, updated_at=role.updated_at,
    )


def _member(user: User, assignment: StaffRoleAssignment | None) -> StaffMemberRead:
    return StaffMemberRead(
        user_id=user.id, username=user.username, email=user.email,
        role=RoleSummary.model_validate(assignment.role) if assignment is not None else None,
        assigned_at=assignment.assigned_at if assignment is not None else None,
    )


@roles_router.get("/access/me", response_model=MyAccess)
async def my_access(user: CurrentUser, access: AccessServiceDep, response: Response) -> MyAccess:
    """Any signed-in user. A customer gets is_staff false and no permissions."""
    response.headers["Cache-Control"] = "no-store"
    permissions, role = await access.resolve(user)
    return MyAccess(
        is_staff=user.is_active and user.role == UserRole.ADMIN,
        role=RoleSummary.model_validate(role) if role is not None else None,
        permissions=sorted(permissions),
    )


@roles_router.get("/admin/permissions", response_model=list[PermissionRead])
async def list_permissions(_admin: RolesManage) -> list[PermissionRead]:
    return [PermissionRead(key=p, group=p.group) for p in Permission]


@roles_router.get("/admin/roles", response_model=list[RoleRead])
async def list_roles(_admin: RolesManage, service: RoleServiceDep) -> list[RoleRead]:
    return [_role(role, members) for role, members in await service.list_roles()]


@roles_router.post("/admin/roles", response_model=RoleRead, status_code=status.HTTP_201_CREATED)
async def create_role(payload: RoleCreate, admin: RolesManage, service: RoleServiceDep) -> RoleRead:
    role = await service.create_role(admin, name=payload.name, description=payload.description, permissions=payload.permissions)
    return _role(role)


@roles_router.get("/admin/roles/{role_id}", response_model=RoleRead)
async def get_role(role_id: uuid.UUID, _admin: RolesManage, service: RoleServiceDep) -> RoleRead:
    for role, members in await service.list_roles():
        if role.id == role_id:
            return _role(role, members)
    return _role(await service.get_role(role_id))  # raises role_not_found


@roles_router.patch("/admin/roles/{role_id}", response_model=RoleRead)
async def update_role(role_id: uuid.UUID, payload: RoleUpdate, admin: RolesManage, service: RoleServiceDep) -> RoleRead:
    """System roles cannot be changed (422 role_is_system). Removing roles.manage from the last role that grants it to an
    active administrator is refused (409 last_role_manager)."""
    role = await service.update_role(
        admin, role_id, name=payload.name, description=payload.description,
        description_set="description" in payload.model_fields_set, permissions=payload.permissions,
    )
    return _role(role)


@roles_router.delete("/admin/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role(role_id: uuid.UUID, admin: RolesManage, service: RoleServiceDep) -> None:
    """Only a role with no staff members (409 role_in_use) and not a system role (422 role_is_system)."""
    await service.delete_role(admin, role_id)


@roles_router.get("/admin/staff", response_model=StaffPage)
async def list_staff(_admin: RolesManage, service: RoleServiceDep, params: PageParams = Depends()) -> StaffPage:
    """Active administrators and their roles. Making someone an administrator stays PATCH /users/{id} (role); they start
    with the No access role until one is assigned here."""
    members, total = await service.list_staff(offset=params.offset, limit=params.page_size)
    return StaffPage(items=[_member(u, a) for u, a in members], total=total, page=params.page, page_size=params.page_size)


@roles_router.put("/admin/staff/{user_id}/role", response_model=StaffMemberRead)
async def assign_role(user_id: uuid.UUID, payload: RoleAssignRequest, admin: RolesManage, service: RoleServiceDep) -> StaffMemberRead:
    """Sets an active administrator's role (404 staff_member_not_found otherwise). Refused if no active administrator would
    keep roles.manage (409 last_role_manager)."""
    user, assignment = await service.assign_role(admin, user_id, payload.role_id)
    return _member(user, assignment)
