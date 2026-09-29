from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.shared.mixins import TimestampMixin


class Address(Base, TimestampMixin):
    """
    A user's saved shipping address. Never referenced live by an Order --
    checkout copies these fields onto the order as a point-in-time
    snapshot (see Order.shipping_*), so editing or deleting an address
    here never changes what a past order says it was shipped to.
    """

    __tablename__ = "addresses"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    recipient_name: Mapped[str] = mapped_column(String(200))
    line1: Mapped[str] = mapped_column(String(255))
    line2: Mapped[str | None] = mapped_column(String(255), default=None)
    city: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(100))
    postal_code: Mapped[str] = mapped_column(String(20))
    country: Mapped[str] = mapped_column(String(2))  # ISO 3166-1 alpha-2

    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")