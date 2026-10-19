import asyncio

import stripe

from app.core.config import get_settings
from app.core.payments.exceptions import (
    InvalidWebhookSignatureError,
    PaymentProviderError,
    PaymentProviderTimeoutError,
)
from app.core.payments.interface import (
    PaymentIntentResult,
    PaymentProvider,
    RefundResult,
    WebhookEvent,
)


class StripePaymentProvider(PaymentProvider):
    name = "stripe"

    def __init__(self, *, secret_key: str, webhook_secret: str) -> None:
        self._webhook_secret = webhook_secret
        stripe.api_key = secret_key

    @staticmethod
    async def _call(function, *args, **kwargs):
        """Runs a blocking Stripe call in a thread, waiting at most STRIPE_TIMEOUT_SECONDS.

        On timeout the thread may still finish, so the outcome is UNKNOWN: we raise
        PaymentProviderTimeoutError and the caller reconciles. Idempotency keys make re-trying safe.
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(function, *args, **kwargs), timeout=get_settings().stripe_timeout_seconds
            )
        except TimeoutError as exc:
            raise PaymentProviderTimeoutError() from exc

    async def cancel_payment_intent(
        self, *, provider_payment_id: str, idempotency_key: str
    ) -> PaymentIntentResult:
        try:
            intent = await self._call(
                stripe.PaymentIntent.cancel, provider_payment_id, idempotency_key=idempotency_key
            )
        except stripe.StripeError:
            # Most often "already succeeded"/"already canceled": ask Stripe what the state really is.
            try:
                intent = await self._call(stripe.PaymentIntent.retrieve, provider_payment_id)
            except stripe.StripeError as exc:
                raise PaymentProviderError(str(exc) or "Failed to cancel payment intent") from exc
        return PaymentIntentResult(
            provider_payment_id=intent.id, client_secret=intent.client_secret, status=intent.status
        )

    async def create_payment_intent(
        self, *, amount_cents: int, currency: str, metadata: dict, idempotency_key: str
    ) -> PaymentIntentResult:
        try:
            intent = await self._call(
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
            intent = await self._call(stripe.PaymentIntent.retrieve, provider_payment_id)
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
        self, *, provider_payment_id: str, amount_cents: int, currency: str, idempotency_key: str
    ) -> RefundResult:
        # A refund inherits the PaymentIntent's currency; Stripe takes no currency parameter. The
        # caller passes the ORIGINAL payment currency and we check Stripe agrees with it.
        try:
            refund = await self._call(
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
            currency=(getattr(refund, "currency", None) or "").lower() or None,
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

        # The SDK returns StripeObject (not a dict: no .get). Work on plain dicts from here on.
        event = event.to_dict() if hasattr(event, "to_dict") else event
        obj = event["data"]["object"]
        currency = obj.get("currency")
        currency = currency.lower() if isinstance(currency, str) else None
        if event["type"].startswith("charge.dispute."):
            return WebhookEvent(
                event_id=event["id"],
                event_type=event["type"],
                provider_payment_id=(obj.get("payment_intent") or ""),
                amount=obj.get("amount"),
                currency=currency,
                provider_dispute_id=obj.get("id"),
                dispute_status=obj.get("status"),
                dispute_reason=obj.get("reason"),
            )
        if event["type"].startswith("refund."):
            # The object is a Refund: its own id, and the PaymentIntent it refunds.
            return WebhookEvent(
                event_id=event["id"],
                event_type=event["type"],
                provider_payment_id=obj.get("payment_intent") or "",
                amount=obj.get("amount"),
                currency=currency,
                provider_refund_id=obj.get("id"),
                refund_status=obj.get("status"),
            )
        return WebhookEvent(
            event_id=event["id"],
            event_type=event["type"],
            provider_payment_id=obj["id"],
            amount=obj.get("amount"),
            amount_received=obj.get("amount_received"),
            currency=currency,
        )
