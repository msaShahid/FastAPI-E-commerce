import uuid
from datetime import datetime

from pydantic import BaseModel


class AuditLogRead(BaseModel):
    id: int
    actor_user_id: uuid.UUID | None
    action: str
    target_type: str
    target_id: str | None
    details: dict | None
    created_at: datetime

    model_config = {"from_attributes": True}
