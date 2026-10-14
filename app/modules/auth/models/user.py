import uuid

from sqlalchemy import Boolean, Enum, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.shared.enums.roles import UserRole
from app.shared.mixins import TimestampMixin


class User(Base, TimestampMixin):
    __tablename__ = "users"
    # Admin user list is newest-first.
    __table_args__ = (Index("ix_users_created_at", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    username: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)

    password_hash: Mapped[str] = mapped_column(String(255))

    role: Mapped[UserRole] = mapped_column(
        Enum(
            UserRole,
            name="user_role",
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=UserRole.USER,
        server_default="user",
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")