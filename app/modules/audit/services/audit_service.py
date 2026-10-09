import uuid

from app.modules.audit.models.audit_log import AuditLog
from app.modules.audit.repositories.audit_repository import AuditRepository


class AuditService:
    def __init__(self, repository: AuditRepository) -> None:
        self.repository = repository

    async def log(
        self,
        *,
        actor_user_id: uuid.UUID | None,
        action: str,
        target_type: str,
        target_id: str | int | uuid.UUID | None,
        details: dict | None = None,
    ) -> AuditLog:
        return await self.repository.create(
            actor_user_id=actor_user_id,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            details=details,
        )

    async def list_logs(
        self,
        *,
        offset: int,
        limit: int,
        action: str | None = None,
        target_type: str | None = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> tuple[list[AuditLog], int]:
        # No role check here: this is only reachable via GET /audit-logs,
        # which already gates on AdminUser at the router, same pattern as
        # e.g. category/product admin-only endpoints.
        return await self.repository.list_paginated(
            offset=offset,
            limit=limit,
            action=action,
            target_type=target_type,
            actor_user_id=actor_user_id,
        )
