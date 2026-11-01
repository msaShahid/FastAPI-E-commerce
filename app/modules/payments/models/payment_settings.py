import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, SmallInteger, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.shared.mixins import TimestampMixin


class PaymentSettings(Base, TimestampMixin):
    """The store's payment mode switches (one row). Live payments are OFF until an administrator, having configured, verified and tested a
    live gateway, explicitly activates them in the admin panel. Deactivating leaves the store with NO preferred provider: nothing
    silently takes over (in particular, never the demo provider)."""

    __tablename__ = "payment_settings"
    __table_args__ = (CheckConstraint("id = 1", name="ck_payment_settings_single_row"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False, default=1)
    live_payments_active: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    live_activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    live_activated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), default=None)
    live_activated_config_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("payment_provider_configs.id", ondelete="SET NULL"), default=None)
    last_deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)
