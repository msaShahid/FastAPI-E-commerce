import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, SmallInteger, String
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

    # Set only when this token was revoked BY ROTATION (a refresh), never by logout, a password change or reuse detection.
    # Within the reuse grace window a replay of such a token is a benign race, not theft.
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    grace_replays: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")
