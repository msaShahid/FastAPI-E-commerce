from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class PaymentIntentResult:
    provider_payment_id: str
    client_secret: str


@dataclass
class WebhookEvent:

    event_id: str
    event_type: str  # e.g. "payment_intent.succeeded"
    provider_payment_id: str


class PaymentProvider(ABC):
    @abstractmethod
    async def create_payment_intent(
        self, *, amount_cents: int, currency: str, metadata: dict
    ) -> PaymentIntentResult:
        raise NotImplementedError

    @abstractmethod
    def verify_and_parse_webhook(
        self, *, payload: bytes, signature_header: str
    ) -> WebhookEvent:
        raise NotImplementedError
