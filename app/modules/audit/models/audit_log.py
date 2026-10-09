import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.shared.mixins import TimestampMixin


class AuditLog(Base, TimestampMixin):
    """
    An append-only record of admin actions, following the same pattern
    as OrderStatusHistory. Nothing ever updates or deletes a row here --
    each admin action that matters (deactivating a user, changing a
    role, refunding a payment, mutating a product/category) writes one
    of these in the SAME database transaction as the action itself
    (both go through the same request-scoped AsyncSession), so an
    action and its audit entry always succeed or fail together -- there
    is no window where one exists without the other.
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action: Mapped[str] = mapped_column(String(100), index=True)
    target_type: Mapped[str] = mapped_column(String(50), index=True)
    target_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
