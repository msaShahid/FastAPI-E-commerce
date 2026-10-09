import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.models.audit_log import AuditLog


class AuditRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self,
        *,
        actor_user_id: uuid.UUID | None,
        action: str,
        target_type: str,
        target_id: str | None,
        details: dict | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            actor_user_id=actor_user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details,
        )
        self.db.add(entry)
        await self.db.flush()
        return entry

    async def list_paginated(
        self,
        *,
        offset: int,
        limit: int,
        action: str | None = None,
        target_type: str | None = None,
        actor_user_id: uuid.UUID | None = None,
    ) -> tuple[list[AuditLog], int]:
        query = select(AuditLog)
        count_query = select(func.count()).select_from(AuditLog)

        if action is not None:
            query = query.where(AuditLog.action == action)
            count_query = count_query.where(AuditLog.action == action)
        if target_type is not None:
            query = query.where(AuditLog.target_type == target_type)
            count_query = count_query.where(AuditLog.target_type == target_type)
        if actor_user_id is not None:
            query = query.where(AuditLog.actor_user_id == actor_user_id)
            count_query = count_query.where(AuditLog.actor_user_id == actor_user_id)

        total = (await self.db.execute(count_query)).scalar_one()

        result = await self.db.execute(
            query.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit)
        )
        items = list(result.scalars().all())

        return items, total
