import uuid
from uuid import UUID

from sqlalchemy import Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.shared.enums.payment_status import PaymentStatus
from app.shared.mixins import TimestampMixin


class Payment(Base, TimestampMixin):


    __tablename__ = "payments"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    order_id: Mapped[UUID] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"), index=True)

    provider: Mapped[str] = mapped_column(String(50), default="stripe", server_default="stripe")
    provider_payment_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)

    status: Mapped[PaymentStatus] = mapped_column(
        Enum(
            PaymentStatus,
            name="payment_status",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        default=PaymentStatus.PENDING,
        server_default=PaymentStatus.PENDING.value,
    )

    amount_cents: Mapped[int] = mapped_column(Integer)