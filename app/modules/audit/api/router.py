import uuid

from fastapi import APIRouter, Depends

from app.modules.audit.dependencies.audit_deps import AuditServiceDep
from app.modules.audit.schemas.audit import AuditLogRead
from app.modules.auth.dependencies.auth import AdminUser
from app.shared.pagination.schemas import PageParams, PaginatedResponse

audit_router = APIRouter(prefix="/audit-logs", tags=["audit"])


@audit_router.get("", response_model=PaginatedResponse[AuditLogRead])
async def list_audit_logs(
    _admin: AdminUser,
    service: AuditServiceDep,
    params: PageParams = Depends(),
    action: str | None = None,
    target_type: str | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> PaginatedResponse[AuditLogRead]:
    logs, total = await service.list_logs(
        offset=params.offset,
        limit=params.page_size,
        action=action,
        target_type=target_type,
        actor_user_id=actor_user_id,
    )
    return PaginatedResponse(
        items=[AuditLogRead.model_validate(log) for log in logs],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )
