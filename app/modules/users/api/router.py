import uuid

from fastapi import APIRouter, Depends, status

from app.modules.audit.dependencies.audit_deps import AuditServiceDep
from app.modules.auth.dependencies.auth import CurrentUser
from app.modules.auth.schemas.auth import UserRead
from app.modules.users.dependencies.user_deps import UserServiceDep
from app.modules.users.schemas.user import UserUpdate
from app.shared.enums.roles import UserRole
from app.shared.pagination.schemas import PageParams, PaginatedResponse

users_router = APIRouter(prefix="/users", tags=["users"])


@users_router.get("/me", response_model=UserRead)
async def get_my_profile(current_user: CurrentUser) -> UserRead:
    return UserRead.model_validate(current_user)


@users_router.get("", response_model=PaginatedResponse[UserRead])
async def list_users(
    current_user: CurrentUser,
    service: UserServiceDep,
    params: PageParams = Depends(),
) -> PaginatedResponse[UserRead]:
    users, total = await service.list_users(
        current_user=current_user,
        offset=params.offset,
        limit=params.page_size,
    )

    return PaginatedResponse(
        items=[UserRead.model_validate(u) for u in users],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@users_router.get("/{user_id}", response_model=UserRead)
async def get_user(
    user_id: uuid.UUID,
    current_user: CurrentUser,
    service: UserServiceDep,
) -> UserRead:
    user = await service.get_user(
        target_user_id=user_id,
        current_user=current_user,
    )

    return UserRead.model_validate(user)


@users_router.patch("/{user_id}", response_model=UserRead)
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    current_user: CurrentUser,
    service: UserServiceDep,
    audit_service: AuditServiceDep,
) -> UserRead:
    # A role change is only reachable when current_user is an admin (see
    # UserService.update_user) -- worth its own audit entry with the
    # before/after role, distinct from an ordinary profile edit.
    previous_role: UserRole | None = None
    if payload.role is not None:
        existing = await service.get_user(target_user_id=user_id, current_user=current_user)
        previous_role = existing.role

    user = await service.update_user(
        target_user_id=user_id,
        current_user=current_user,
        username=payload.username,
        email=payload.email,
        role=payload.role,
    )

    if payload.role is not None and previous_role != user.role:
        await audit_service.log(
            actor_user_id=current_user.id,
            action="user.role_changed",
            target_type="user",
            target_id=user.id,
            details={"from": previous_role.value, "to": user.role.value},
        )

    return UserRead.model_validate(user)


@users_router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def deactivate_user(
    user_id: uuid.UUID,
    current_user: CurrentUser,
    service: UserServiceDep,
    audit_service: AuditServiceDep,
) -> None:
    await service.deactivate_user(
        target_user_id=user_id,
        current_user=current_user,
    )

    await audit_service.log(
        actor_user_id=current_user.id,
        action="user.deactivated",
        target_type="user",
        target_id=user_id,
        details={"self_service": current_user.id == user_id},
    )
