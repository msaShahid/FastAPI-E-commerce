"""The built-in DEMO payment gateway: a faithful simulator of a real gateway (payment intents, signed webhook events, refunds) that never
touches real money and never receives real card details.

  * It implements the same PaymentProvider interface as Stripe and Razorpay, so a demo payment travels through the SAME checkout, payment
    verification (signed event, amount and currency checks, de-duplication), order lifecycle, refund and reconciliation code as a live one.
  * It can only be a TEST-mode configuration (the database refuses anything else) and its events are signed with its own secret, so a demo
    event can never mark a payment made with another provider as paid.
  * A "customer" pays with FAKE test details (the cards, UPI ids and banks listed in TEST_DETAILS). A card number that is not one of the
    listed test cards is refused outright, before anything is recorded: real card numbers are never accepted, stored or logged. Only the
    outcome (and the last four digits of a test card) is kept."""

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.payments.exceptions import (
    InvalidWebhookPayloadError,
    InvalidWebhookSignatureError,
    PaymentProviderError,
)
from app.core.payments.interface import (
    CredentialCheck,
    PaymentIntentResult,
    PaymentProvider,
    RefundResult,
    WebhookEvent,
)

SIGNATURE_HEADER = "x-demo-signature"
TOLERANCE_SECONDS = 300


@dataclass(frozen=True)
class DemoOutcome:
    succeeds: bool
    failure_code: str | None = None
    message: str = ""


# The ONLY details the demo gateway accepts. They are public, fake, and printed by GET /payments/demo/test-details.
TEST_CARDS: dict[str, tuple[DemoOutcome, str]] = {
    "4242424242424242": (DemoOutcome(True, None, "Payment successful"), "Visa test card: always succeeds"),
    "5555555555554444": (DemoOutcome(True, None, "Payment successful"), "Mastercard test card: always succeeds"),
    "4000000000000002": (DemoOutcome(False, "card_declined", "The card was declined"), "Always declined"),
    "4000000000009995": (DemoOutcome(False, "insufficient_funds", "Insufficient funds"), "Declined: insufficient funds"),
    "4000000000000069": (DemoOutcome(False, "expired_card", "The card has expired"), "Declined: expired card"),
    "4000000000000119": (DemoOutcome(False, "processing_error", "A processing error occurred"), "Declined: processing error"),
}
TEST_UPI: dict[str, tuple[DemoOutcome, str]] = {
    "success@demo": (DemoOutcome(True, None, "Payment successful"), "Demo UPI id: succeeds"),
    "failure@demo": (DemoOutcome(False, "upi_declined", "The UPI payment was declined"), "Demo UPI id: declined"),
}
TEST_NETBANKING: dict[str, tuple[DemoOutcome, str]] = {
    "demo_bank_success": (DemoOutcome(True, None, "Payment successful"), "Demo bank: succeeds"),
    "demo_bank_failure": (DemoOutcome(False, "bank_declined", "The bank declined the payment"), "Demo bank: declined"),
}


def normalise_card(number: str) -> str:
    return "".join(ch for ch in number if ch.isdigit())


def sign(secret: str, payload: bytes, *, timestamp: int | None = None) -> str:
    """The signature header for a demo event: `t=<unix time>,v1=<HMAC-SHA256 of "<t>.<payload>">`."""
    t = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={digest}"


def build_event(*, event_id: str, event_type: str, provider_payment_id: str, amount_cents: int, currency: str) -> bytes:
    return json.dumps({
        "id": event_id, "type": event_type,
        "data": {"id": provider_payment_id, "amount": amount_cents, "amount_received": amount_cents if event_type == "payment_intent.succeeded" else 0,
                 "currency": currency.lower()},
    }, separators=(",", ":"), sort_keys=True).encode()


class DemoPaymentProvider(PaymentProvider):
    name = "demo"
    supports_cancel = True

    def __init__(self, webhook_secret: str | None = None, session: AsyncSession | None = None) -> None:
        self._webhook_secret = webhook_secret
        self._session = session

    def bind_session(self, session: AsyncSession) -> "DemoPaymentProvider":
        """The demo gateway keeps its (tiny) state in the database, in the caller's own transaction."""
        self._session = session
        return self

    def _db(self) -> AsyncSession:
        if self._session is None:
            raise PaymentProviderError("The demo gateway has no database session")
        return self._session

    async def validate_credentials(self) -> CredentialCheck:
        return CredentialCheck(valid=True, reason=None, detected_mode="test")  # there are no credentials: it is always usable

    async def create_payment_intent(self, *, amount_cents: int, currency: str, metadata: dict, idempotency_key: str) -> PaymentIntentResult:
        from app.modules.payments.models.demo_intent import DemoPaymentIntent

        token = hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]  # the same key always yields the same intent, like a real gateway
        intent_id = f"pi_demo_{token}"
        await self._db().execute(
            pg_insert(DemoPaymentIntent).values(id=intent_id, amount_cents=amount_cents, currency=currency.lower())
            .on_conflict_do_nothing(index_elements=[DemoPaymentIntent.id])
        )
        intent = (await self._db().execute(select(DemoPaymentIntent).where(DemoPaymentIntent.id == intent_id))).scalar_one()
        return PaymentIntentResult(provider_payment_id=intent_id, client_secret=f"demo_secret_{token}", status=intent.status)

    async def _intent(self, provider_payment_id: str):
        from app.modules.payments.models.demo_intent import DemoPaymentIntent

        intent = (await self._db().execute(
            select(DemoPaymentIntent).where(DemoPaymentIntent.id == provider_payment_id).with_for_update())).scalar_one_or_none()
        if intent is None:
            raise PaymentProviderError("Unknown demo payment")
        return intent

    async def retrieve_payment_intent(self, *, provider_payment_id: str) -> PaymentIntentResult:
        intent = await self._intent(provider_payment_id)
        return PaymentIntentResult(provider_payment_id=intent.id, client_secret="", status=intent.status)

    async def cancel_payment_intent(self, *, provider_payment_id: str, idempotency_key: str) -> PaymentIntentResult:
        intent = await self._intent(provider_payment_id)
        if intent.status == "requires_payment_method":
            intent.status = "canceled"
            await self._db().flush()
        return PaymentIntentResult(provider_payment_id=intent.id, client_secret="", status=intent.status)  # succeeded stays succeeded

    async def record_outcome(self, provider_payment_id: str, outcome: DemoOutcome) -> str:
        """The customer 'paid' (or failed to) with test details. Returns the intent's resulting status."""
        intent = await self._intent(provider_payment_id)
        if intent.status == "canceled":
            raise PaymentProviderError("This demo payment was cancelled")
        if outcome.succeeds:
            intent.status, intent.last_failure_code = "succeeded", None
        elif intent.status != "succeeded":
            intent.last_failure_code = outcome.failure_code
        await self._db().flush()
        return intent.status

    def verify_and_parse_webhook(self, *, payload: bytes, signature_header: str) -> WebhookEvent:
        if not self._webhook_secret:
            raise InvalidWebhookSignatureError()
        try:
            parts = dict(item.split("=", 1) for item in signature_header.split(","))
            timestamp, given = int(parts["t"]), parts["v1"]
        except (ValueError, KeyError):
            raise InvalidWebhookSignatureError() from None
        expected = hmac.new(self._webhook_secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, given) or abs(time.time() - timestamp) > TOLERANCE_SECONDS:
            raise InvalidWebhookSignatureError()
        try:
            event = json.loads(payload)
            data = event["data"]
            return WebhookEvent(
                event_id=str(event["id"]), event_type=str(event["type"]), provider_payment_id=str(data["id"]), amount=data.get("amount"),
                amount_received=data.get("amount_received"), currency=str(data["currency"]).lower() if data.get("currency") else None, provider="demo",
            )
        except (ValueError, KeyError, TypeError, AttributeError):
            raise InvalidWebhookPayloadError() from None

    async def create_refund(self, *, provider_payment_id: str, amount_cents: int, currency: str, idempotency_key: str) -> RefundResult:
        token = hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
        return RefundResult(provider_refund_id=f"re_demo_{token}", amount_cents=amount_cents, status="succeeded", currency=currency.lower())


def new_event_id() -> str:
    return f"evt_demo_{secrets.token_hex(12)}"
