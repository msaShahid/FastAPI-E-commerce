import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.roles.permissions import Permission


class PermissionRead(BaseModel):
    key: Permission
    group: str


class RoleSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    is_system: bool


class RoleRead(RoleSummary):
    description: str | None
    permissions: list[str] = Field(description="The permissions given. For the Owner role: every permission, including ones added later.")
    effective_permissions: list[str] = Field(description="What the role can actually do: the permissions given plus the reads they include.")
    member_count: int = 0
    created_at: datetime
    updated_at: datetime


class MyAccess(BaseModel):
    """What the signed-in user may do. Use it to shape the admin UI only: every staff endpoint checks again."""

    is_staff: bool
    role: RoleSummary | None = Field(description="Null for a customer, and for an administrator with no role (every permission).")
    permissions: list[Permission]


def _clean_name(value: str) -> str:
    value = " ".join(value.split())
    if not value:
        raise ValueError("A role needs a name")
    return value


class RoleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    description: str | None = Field(default=None, max_length=500)
    permissions: set[Permission] = Field(default_factory=set)

    _name = field_validator("name")(_clean_name)


class RoleUpdate(BaseModel):
    """Only the fields sent are changed. Send description: null to clear it."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=60)
    description: str | None = Field(default=None, max_length=500)
    permissions: set[Permission] | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return None if value is None else _clean_name(value)


class StaffMemberRead(BaseModel):
    user_id: uuid.UUID
    username: str
    email: str
    role: RoleSummary | None = Field(description="Null: an administrator with no role assigned, who holds every permission.")
    assigned_at: datetime | None


class StaffPage(BaseModel):
    items: list[StaffMemberRead]
    total: int
    page: int
    page_size: int


class RoleAssignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_id: uuid.UUID
