import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.shared.mixins import TimestampMixin


class EmailProviderConfig(Base, TimestampMixin):
    """One email service configured for THIS store, with its credentials ENCRYPTED (app/core/secrets.py): only ciphertext and
    non-secret hints are stored here. At most one is ACTIVE; the database enforces it, so two administrators switching at the same
    moment cannot leave two. With none active, the deployment's environment settings (EMAIL_BACKEND) apply."""

    __tablename__ = "email_provider_configs"
    __table_args__ = (
        UniqueConstraint("provider", name="uq_email_provider_configs_provider"),
        Index("ux_email_provider_configs_one_active", "is_active", unique=True, postgresql_where=text("is_active")),
        CheckConstraint("provider IN ('google', 'zoho', 'mailtrap')", name="ck_email_provider_configs_provider"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(20))
    mode: Mapped[str | None] = mapped_column(String(20), default=None)   # zoho data centre / mailtrap product
    credentials_ciphertext: Mapped[str] = mapped_column(Text)            # encrypted JSON {"username": ..., "password": ...}
    username_hint: Mapped[str] = mapped_column(String(100))              # masked, e.g. s***@gmail.com
    password_hint: Mapped[str] = mapped_column(String(12))               # "...ab12": never enough to use the secret
    from_email: Mapped[str] = mapped_column(String(255))
    from_name: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    last_test_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, default=None)
    last_test_error: Mapped[str | None] = mapped_column(String(40), default=None)   # a fixed code, never server text
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_error_code: Mapped[str | None] = mapped_column(String(40), default=None)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), default=None)
