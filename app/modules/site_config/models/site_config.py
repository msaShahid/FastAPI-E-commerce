import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, SmallInteger, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class SiteConfigVersion(Base):
    """One PUBLISHED storefront configuration, kept for good: history is append-only (a rollback publishes a copy as a NEW version)."""

    __tablename__ = "site_config_versions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    version: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    document: Mapped[dict] = mapped_column(JSONB)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SiteConfigState(Base):
    """The single row: which version the storefront serves, and the working draft an administrator is editing."""

    __tablename__ = "site_config_state"
    __table_args__ = (CheckConstraint("id = 1", name="ck_site_config_state_singleton"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    published_version: Mapped[int | None] = mapped_column(Integer, default=None)
    draft: Mapped[dict | None] = mapped_column(JSONB, default=None)
    draft_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    draft_updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), default=None)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
