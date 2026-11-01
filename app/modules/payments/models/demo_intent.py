from sqlalchemy import CheckConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.shared.mixins import TimestampMixin

DEMO_STATUSES = ("requires_payment_method", "succeeded", "canceled")


class DemoPaymentIntent(Base, TimestampMixin):
    """What the built-in demo gateway 'remembers' about a payment it was asked to take (the role Stripe's or Razorpay's servers play for a
    real gateway). It holds NO card or account details: only the amount, currency and outcome. No real money is ever involved."""

    __tablename__ = "demo_payment_intents"
    __table_args__ = (
        CheckConstraint(f"status IN ({', '.join(repr(s) for s in DEMO_STATUSES)})", name="ck_demo_payment_intents_status"),
        CheckConstraint("amount_cents > 0", name="ck_demo_payment_intents_amount"),
    )

    id: Mapped[str] = mapped_column(String(255), primary_key=True)          # the demo gateway's payment id: pi_demo_...
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(30), default="requires_payment_method", server_default="requires_payment_method")
    last_failure_code: Mapped[str | None] = mapped_column(String(40), default=None)
