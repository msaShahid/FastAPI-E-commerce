import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.shared.mixins import TimestampMixin


class RefreshToken(Base, TimestampMixin):
    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    jti: Mapped[str] = mapped_column(String(36), unique=True, index=True)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )

    family_id: Mapped[uuid.UUID] = mapped_column(index=True)
    
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    revoked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")