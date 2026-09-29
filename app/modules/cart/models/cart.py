from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.shared.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.modules.cart.models.cart_item import CartItem


class Cart(Base, TimestampMixin):

    __tablename__ = "carts"
    __table_args__ = (
        CheckConstraint(
            "(user_id IS NOT NULL AND guest_token IS NULL) OR "
            "(user_id IS NULL AND guest_token IS NOT NULL)",
            name="ck_carts_user_xor_guest",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, default=None
    )
    guest_token: Mapped[str | None] = mapped_column(
        String(64), unique=True, index=True, default=None
    )

    items: Mapped[list["CartItem"]] = relationship(
        back_populates="cart", cascade="all, delete-orphan"
    )
