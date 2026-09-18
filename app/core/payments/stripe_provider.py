import stripe

from app.core.payments.exceptions import InvalidWebhookSignatureError
from app.core.payments.interface import (
    PaymentIntentResult,
    PaymentProvider,
    WebhookEvent,
)


class StripePaymentProvider(PaymentProvider):
    def __init__(self, *, secret_key: str, webhook_secret: str) -> None:
        self._webhook_secret = webhook_secret
        stripe.api_key = secret_key

    async def create_payment_intent(
        self, *, amount_cents: int, currency: str, metadata: dict
    ) -> PaymentIntentResult:
    
        intent = stripe.PaymentIntent.create(
            amount=amount_cents,
            currency=currency,
            metadata=metadata,
        )
        return PaymentIntentResult(
            provider_payment_id=intent.id, client_secret=intent.client_secret
        )

    def verify_and_parse_webhook(
        self, *, payload: bytes, signature_header: str
    ) -> WebhookEvent:
        try:
            event = stripe.Webhook.construct_event(
                payload, signature_header, self._webhook_secret
            )
        except (stripe.error.SignatureVerificationError, ValueError) as exc:
            raise InvalidWebhookSignatureError() from exc

        payment_intent_id = event["data"]["object"]["id"]
        return WebhookEvent(
            event_id=event["id"],
            event_type=event["type"],
            provider_payment_id=payment_intent_id,
        )
