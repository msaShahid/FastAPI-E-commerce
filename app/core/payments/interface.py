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


@dataclass
class RefundResult:
    provider_refund_id: str
    amount_cents: int
    status: str  # provider-native status, e.g. Stripe's "succeeded"


class PaymentProvider(ABC):
    @abstractmethod
    async def create_payment_intent(
        self, *, amount_cents: int, currency: str, metadata: dict, idempotency_key: str
    ) -> PaymentIntentResult:
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
        self, *, provider_payment_id: str, amount_cents: int, idempotency_key: str
    ) -> RefundResult:
        raise NotImplementedError