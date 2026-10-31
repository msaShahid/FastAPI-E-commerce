import uuid

from sqlalchemy import CheckConstraint, ForeignKey, SmallInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.shared.mixins import TimestampMixin


class BusinessProfile(Base, TimestampMixin):
    """The seller's details printed on tax invoices (one row: one deployment is one business). The legal name and GSTIN are NOT here:
    they come from the approved GST registration recorded with each order. Changing this never alters an invoice already issued."""

    __tablename__ = "business_profile"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_business_profile_single_row"),
        CheckConstraint("pincode ~ '^[0-9]{6}$'", name="ck_business_profile_pincode"),
        CheckConstraint("invoice_prefix ~ '^[A-Z0-9]{1,5}$'", name="ck_business_profile_prefix"),
        CheckConstraint("state_code ~ '^[0-9]{2}$'", name="ck_business_profile_state"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False, default=1)  # always 1: no sequence
    trade_name: Mapped[str] = mapped_column(String(200))
    address_line1: Mapped[str] = mapped_column(String(255))
    address_line2: Mapped[str | None] = mapped_column(String(255), default=None)
    city: Mapped[str] = mapped_column(String(100))
    state_code: Mapped[str] = mapped_column(String(2))
    pincode: Mapped[str] = mapped_column(String(6))
    email: Mapped[str | None] = mapped_column(String(255), default=None)
    phone: Mapped[str | None] = mapped_column(String(30), default=None)
    invoice_prefix: Mapped[str] = mapped_column(String(5), default="INV", server_default="INV")
    footer: Mapped[str | None] = mapped_column(Text, default=None)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), default=None)
