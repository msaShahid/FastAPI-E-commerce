from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class PaymentIntentResult:
    provider_payment_id: str
    client_secret: str
    status: str  # provider-native status, e.g. Stripe's "requires_payment_method"


@dataclass
class WebhookEvent:

    event_id: str
    event_type: str  # e.g. "payment_intent.succeeded"
    provider_payment_id: str
    # What the provider says was charged: integer minor units and lower-case ISO currency.
    # None means "not provided"; the payment service treats that as unverifiable, never as OK.
    amount: int | None = None
    amount_received: int | None = None
    currency: str | None = None
    # Refund events (event_type "refund.*"): provider_payment_id is the refunded PaymentIntent.
    provider_refund_id: str | None = None
    refund_status: str | None = None
    # Dispute events ("charge.dispute.*"): provider_payment_id is the disputed PaymentIntent (may be empty).
    provider_dispute_id: str | None = None
    dispute_status: str | None = None
    dispute_reason: str | None = None
    # Which provider sent it. Event ids are only unique per provider, so this scopes de-duplication.
    provider: str = "stripe"


@dataclass
class RefundResult:
    provider_refund_id: str
    amount_cents: int
    status: str  # provider-native status, e.g. Stripe's "succeeded"
    currency: str | None = None


class PaymentProvider(ABC):
    # Recorded on every payment and webhook event so they can be reconciled after provider settings change.
    # The default exists because Stripe is the only provider today; every new provider sets its own name.
    name: str = "stripe"

    def supports_currency(self, code: str) -> bool:
        """Whether payments in this currency are verified for the integration (see app/core/money.py)."""
        from app.core.money import CURRENCIES

        currency = CURRENCIES.get(code.strip().lower())
        return bool(currency and currency.payment_verified)

    @abstractmethod
    async def create_payment_intent(
        self, *, amount_cents: int, currency: str, metadata: dict, idempotency_key: str
    ) -> PaymentIntentResult:
        raise NotImplementedError

    @abstractmethod
    async def cancel_payment_intent(
        self, *, provider_payment_id: str, idempotency_key: str
    ) -> PaymentIntentResult:
        """Makes the intent unpayable and returns its FINAL provider status.

        If it cannot be cancelled because it already succeeded (or was already cancelled), the real
        status is returned, not an error. Raises PaymentProviderError/PaymentProviderTimeoutError
        only when the outcome is genuinely unknown."""
        raise NotImplementedError

    @abstractmethod
    async def retrieve_payment_intent(
        self, *, provider_payment_id: str
    ) -> PaymentIntentResult:
        raise NotImplementedError

    @abstractmethod
    def verify_and_parse_webhook(
        self, *, payload: bytes, signature_header: str
    ) -> WebhookEvent:
        raise NotImplementedError

    @abstractmethod
    async def create_refund(
        self, *, provider_payment_id: str, amount_cents: int, currency: str, idempotency_key: str
    ) -> RefundResult:
        raise NotImplementedError