import asyncio

import stripe

from app.core.payments.exceptions import (
    InvalidWebhookSignatureError,
    PaymentProviderError,
)
from app.core.payments.interface import (
    PaymentIntentResult,
    PaymentProvider,
    RefundResult,
    WebhookEvent,
)


class StripePaymentProvider(PaymentProvider):
    def __init__(self, *, secret_key: str, webhook_secret: str) -> None:
        self._webhook_secret = webhook_secret
        stripe.api_key = secret_key

    async def create_payment_intent(
        self, *, amount_cents: int, currency: str, metadata: dict, idempotency_key: str
    ) -> PaymentIntentResult:
        try:
            intent = await asyncio.to_thread(
                stripe.PaymentIntent.create,
                amount=amount_cents,
                currency=currency,
                metadata=metadata,
                idempotency_key=idempotency_key,
            )
        except stripe.StripeError as exc:
            raise PaymentProviderError(
                str(exc) or "Failed to create payment intent"
            ) from exc
        return PaymentIntentResult(
            provider_payment_id=intent.id,
            client_secret=intent.client_secret,
            status=intent.status,
        )

    async def retrieve_payment_intent(
        self, *, provider_payment_id: str
    ) -> PaymentIntentResult:
        try:
            intent = await asyncio.to_thread(
                stripe.PaymentIntent.retrieve, provider_payment_id
            )
        except stripe.StripeError as exc:
            raise PaymentProviderError(
                str(exc) or "Failed to retrieve payment intent"
            ) from exc
        return PaymentIntentResult(
            provider_payment_id=intent.id,
            client_secret=intent.client_secret,
            status=intent.status,
        )

    async def create_refund(
        self, *, provider_payment_id: str, amount_cents: int, idempotency_key: str
    ) -> RefundResult:
        try:
            refund = await asyncio.to_thread(
                stripe.Refund.create,
                payment_intent=provider_payment_id,
                amount=amount_cents,
                idempotency_key=idempotency_key,
            )
        except stripe.StripeError as exc:
            raise PaymentProviderError(str(exc) or "Failed to create refund") from exc
        return RefundResult(
            provider_refund_id=refund.id,
            amount_cents=refund.amount,
            status=refund.status,
        )

    def verify_and_parse_webhook(
        self, *, payload: bytes, signature_header: str
    ) -> WebhookEvent:
        try:
            event = stripe.Webhook.construct_event(
                payload, signature_header, self._webhook_secret
            )
        except (stripe.SignatureVerificationError, ValueError) as exc:
            raise InvalidWebhookSignatureError() from exc

        payment_intent_id = event["data"]["object"]["id"]
        return WebhookEvent(
            event_id=event["id"],
            event_type=event["type"],
            provider_payment_id=payment_intent_id,
        )
