import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AuthToken(Base):
    """A single-use, purpose-bound, expiring secret that proves control of an inbox (password reset, email verification, email
    change). Only the SHA-256 of the secret is stored: the secret itself exists only in the email that carries it."""

    __tablename__ = "auth_tokens"
    __table_args__ = (
        CheckConstraint("purpose IN ('password_reset', 'email_verification', 'email_change')", name="ck_auth_tokens_purpose"),
        Index("ix_auth_tokens_user_purpose_created", "user_id", "purpose", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    purpose: Mapped[str] = mapped_column(String(30))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # The address this token was issued FOR: the account's current address (reset, verification) or the NEW one (change). A token is
    # only honoured while it still matches, so a link sent to an address that has since been replaced is dead.
    email: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
