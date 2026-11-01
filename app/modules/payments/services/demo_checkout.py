"""Paying with the demo gateway.

The customer's "payment" is turned into exactly what a real gateway would send: a SIGNED event, delivered to the very same webhook handling a
live Stripe or Razorpay event goes through (signature verified against the demo configuration's stored secret, amount and currency checked,
duplicates ignored, the order marked paid by the normal lifecycle). Only the first step differs: instead of a customer's card reaching a real
gateway, one of the published fake test details reaches the simulator."""

from app.core.money import to_stripe_amount
from app.core.payments.demo_provider import (
    TEST_CARDS,
    TEST_NETBANKING,
    TEST_UPI,
    DemoOutcome,
    DemoPaymentProvider,
    build_event,
    new_event_id,
    normalise_card,
    sign,
)
from app.core.secrets import get_cipher
from app.modules.orders.services.order_service import OrderService
from app.modules.payments.exceptions.demo_exceptions import (
    DemoGatewayInactiveError,
    DemoOnlyTestDetailsError,
    NoDemoPaymentError,
    NotADemoPaymentError,
)
from app.modules.payments.models.payment import PaymentStatus
from app.modules.payments.repositories.payment_repository import PaymentRepository
from app.modules.payments.repositories.provider_config_repository import ProviderConfigRepository
from app.modules.payments.schemas.demo import DemoPayRequest, DemoPayResult, TestDetail, TestDetails
from app.modules.payments.services.payment_service import PaymentService


def resolve_outcome(details: DemoPayRequest) -> DemoOutcome:
    """The outcome of the fake details, or a refusal that says nothing about what was typed."""
    if details.method == "card":
        entry = TEST_CARDS.get(normalise_card(details.card_number or ""))
    elif details.method == "upi":
        entry = TEST_UPI.get((details.upi_id or "").strip().lower())
    else:
        entry = TEST_NETBANKING.get((details.bank or "").strip().lower())
    if entry is None:
        raise DemoOnlyTestDetailsError()
    return entry[0]


def published_test_details(demo_active: bool) -> TestDetails:
    def rows(table, pretty=lambda v: v):
        return [TestDetail(value=pretty(v), outcome="succeeds" if o.succeeds else "declined", description=d) for v, (o, d) in table.items()]

    return TestDetails(
        demo_active=demo_active,
        notice="DEMO MODE: no real money moves. Use ONLY these fake details. The demo gateway refuses anything else, so real card or account details can never be entered.",
        cards=rows(TEST_CARDS, lambda n: " ".join(n[i:i + 4] for i in range(0, 16, 4))),
        upi_ids=rows(TEST_UPI), banks=rows(TEST_NETBANKING),
        other="Expiry date: any future date. CVC: any 3 digits. One-time password: 123456. Do not send these to the API: only the card number, UPI id or bank is sent.",
    )


class DemoCheckoutService:
    def __init__(self, db, payments: PaymentService, orders: OrderService, *, cipher_factory=get_cipher) -> None:
        self.db = db
        self.payments = payments
        self.orders = orders
        self.repository = PaymentRepository(db)
        self.configs = ProviderConfigRepository(db)
        self._cipher_factory = cipher_factory

    async def demo_active(self) -> bool:
        preferred = await self.configs.get_preferred()
        return bool(preferred and preferred.provider == "demo" and preferred.status == "enabled")

    async def pay(self, request: DemoPayRequest, *, user_id, guest_payment_token: str | None) -> DemoPayResult:
        outcome = resolve_outcome(request)  # first, before anything is looked up or recorded
        if user_id is not None:
            order = await self.orders.get_order(order_id=request.order_id, user_id=user_id, is_admin=False)
        else:
            order = await self.orders.authorize_guest_payment(order_id=request.order_id, token=guest_payment_token or "")
        payments = await self.repository.list_for_order(order.id)
        payment = next((p for p in payments if p.status in (PaymentStatus.PENDING, PaymentStatus.FAILED)), None)
        paid = next((p for p in payments if p.status == PaymentStatus.SUCCEEDED), None)
        if paid is not None and paid.provider == "demo":
            return self._result(order, paid, True, None, "This order has already been paid")
        if payment is None:
            raise NoDemoPaymentError()
        if payment.provider != "demo" or payment.provider_config_id is None:
            raise NotADemoPaymentError()
        config = await self.configs.get(payment.provider_config_id)
        if config is None or config.provider != "demo":
            raise NotADemoPaymentError()
        cipher = self._cipher_factory()
        secret = cipher.decrypt(config.webhook_secret_ciphertext)
        adapter = DemoPaymentProvider(webhook_secret=secret, session=self.db)
        await adapter.record_outcome(payment.provider_payment_id, outcome)
        payload = build_event(
            event_id=new_event_id(), event_type="payment_intent.succeeded" if outcome.succeeds else "payment_intent.payment_failed",
            provider_payment_id=payment.provider_payment_id, amount_cents=to_stripe_amount(payment.amount_cents, payment.currency), currency=payment.currency,
        )
        await self.payments.handle_webhook(payload=payload, signature_header=sign(secret, payload), provider_name="demo", headers={})
        refreshed = await self.repository.get_by_id(payment.id)
        fresh_order = await self.orders.get_by_id_unchecked(order.id)
        return self._result(fresh_order, refreshed, refreshed.status == PaymentStatus.SUCCEEDED, outcome.failure_code, outcome.message)

    @staticmethod
    def _result(order, payment, succeeded: bool, failure_code: str | None, message: str) -> DemoPayResult:
        return DemoPayResult(order_id=order.id, payment_id=payment.id, succeeded=succeeded, payment_status=payment.status.value,
                             order_status=order.status.value, failure_code=failure_code, message=message)


__all__ = ["DemoCheckoutService", "DemoGatewayInactiveError", "published_test_details", "resolve_outcome"]
