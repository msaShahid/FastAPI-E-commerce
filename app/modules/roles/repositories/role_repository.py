import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models.user import User
from app.modules.roles.models.staff_role import StaffRole, StaffRoleAssignment
from app.modules.roles.permissions import (
    NO_ACCESS_ROLE_ID,
    NO_ACCESS_ROLE_NAME,
    OWNER_ROLE_ID,
    OWNER_ROLE_NAME,
)
from app.shared.enums.roles import UserRole


class RoleRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_assignment(self, user_id: uuid.UUID) -> StaffRoleAssignment | None:
        return await self.db.scalar(select(StaffRoleAssignment).where(StaffRoleAssignment.user_id == user_id))

    async def ensure_system_roles(self) -> None:
        """The migration creates the system roles; a database built without migrations (tests) gets them on first use."""
        for role_id, name in ((OWNER_ROLE_ID, OWNER_ROLE_NAME), (NO_ACCESS_ROLE_ID, NO_ACCESS_ROLE_NAME)):
            if await self.db.get(StaffRole, role_id) is None:
                self.db.add(StaffRole(id=role_id, name=name, permissions=[], is_system=True))
        await self.db.flush()

    async def list_roles(self) -> list[tuple[StaffRole, int]]:
        members = (
            select(StaffRoleAssignment.role_id, func.count().label("n"))
            .join(User, User.id == StaffRoleAssignment.user_id)
            .where(User.role == UserRole.ADMIN, User.is_active.is_(True))
            .group_by(StaffRoleAssignment.role_id)
            .subquery()
        )
        rows = await self.db.execute(
            select(StaffRole, func.coalesce(members.c.n, 0))
            .outerjoin(members, members.c.role_id == StaffRole.id)
            .order_by(StaffRole.is_system.desc(), StaffRole.name)
        )
        return [(role, int(n)) for role, n in rows.all()]

    async def get_role(self, role_id: uuid.UUID, *, for_update: bool = False) -> StaffRole | None:
        stmt = select(StaffRole).where(StaffRole.id == role_id)
        if for_update:
            stmt = stmt.with_for_update()
        return await self.db.scalar(stmt)

    async def name_taken(self, name: str, *, excluding: uuid.UUID | None = None) -> bool:
        stmt = select(StaffRole.id).where(func.lower(StaffRole.name) == name.lower())
        if excluding is not None:
            stmt = stmt.where(StaffRole.id != excluding)
        return await self.db.scalar(stmt) is not None

    async def count_assignments(self, role_id: uuid.UUID) -> int:
        return int(await self.db.scalar(
            select(func.count()).select_from(StaffRoleAssignment).where(StaffRoleAssignment.role_id == role_id)
        ) or 0)

    async def add(self, role: StaffRole) -> StaffRole:
        self.db.add(role)
        await self.db.flush()
        await self.db.refresh(role)
        return role

    async def save(self, role: StaffRole) -> StaffRole:
        await self.db.flush()
        await self.db.refresh(role)
        return role

    async def delete(self, role: StaffRole) -> None:
        await self.db.delete(role)
        await self.db.flush()

    async def lock_active_admins(self) -> list[tuple[User, StaffRoleAssignment | None]]:
        """Every active administrator with their assignment, admin rows locked: role-manager checks are race-free."""
        admins = list(await self.db.scalars(
            select(User).where(User.role == UserRole.ADMIN, User.is_active.is_(True)).order_by(User.id).with_for_update()
        ))
        if not admins:
            return []
        assignments = {
            a.user_id: a for a in await self.db.scalars(
                select(StaffRoleAssignment).where(StaffRoleAssignment.user_id.in_([u.id for u in admins]))
            )
        }
        return [(u, assignments.get(u.id)) for u in admins]

    async def list_staff(self, *, offset: int, limit: int) -> tuple[list[tuple[User, StaffRoleAssignment | None]], int]:
        base = select(User).where(User.role == UserRole.ADMIN, User.is_active.is_(True))
        total = int(await self.db.scalar(select(func.count()).select_from(base.subquery())) or 0)
        users = list(await self.db.scalars(base.order_by(User.username).offset(offset).limit(limit)))
        assignments = {
            a.user_id: a for a in await self.db.scalars(
                select(StaffRoleAssignment).where(StaffRoleAssignment.user_id.in_([u.id for u in users]))
            )
        } if users else {}
        return [(u, assignments.get(u.id)) for u in users], total

    async def get_active_admin(self, user_id: uuid.UUID) -> User | None:
        return await self.db.scalar(
            select(User).where(User.id == user_id, User.role == UserRole.ADMIN, User.is_active.is_(True))
        )

    async def set_assignment(self, *, user_id: uuid.UUID, role_id: uuid.UUID, actor_id: uuid.UUID) -> StaffRoleAssignment:
        assignment = await self.get_assignment(user_id)
        if assignment is None:
            assignment = StaffRoleAssignment(user_id=user_id, role_id=role_id, assigned_by_user_id=actor_id)
            self.db.add(assignment)
        else:
            assignment.role_id = role_id
            assignment.assigned_by_user_id = actor_id
        await self.db.flush()
        await self.db.refresh(assignment, ["role"])
        return assignment

    async def remove_assignment(self, user_id: uuid.UUID) -> None:
        assignment = await self.get_assignment(user_id)
        if assignment is not None:
            await self.db.delete(assignment)
            await self.db.flush()
