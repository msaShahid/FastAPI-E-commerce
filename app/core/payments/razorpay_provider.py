"""
Razorpay adapter.

STATUS: implemented and tested ONLY against mocked HTTP. It has NOT been run against Razorpay's sandbox and is NOT
production-ready. It was written from secondary sources (see docs/PAYMENT_PROVIDERS.md), not verified against Razorpay's
official documentation. Capabilities are declared conservatively in app/core/payments/registry.py.

Mapping to the shared lifecycle (provider-specific details stay in this file):
  * a Razorpay ORDER is our "payment intent": provider_payment_id = order id; `client_secret` = the order id (the checkout
    needs the order id and the PUBLIC key id; the key SECRET never leaves the server);
  * the Razorpay PAYMENT id (pay_...) arrives in the payment.captured webhook and is carried as `provider_reference`; a refund
    needs it;
  * an order cannot be cancelled at Razorpay (supports_cancel = False): expiry records that and relies on the late-payment path;
  * webhooks: HMAC-SHA256 (hex) over the RAW body with the webhook secret (header X-Razorpay-Signature). The scheme carries no
    timestamp, so replays cannot be bounded by time: they are bounded by de-duplicating on x-razorpay-event-id.
Events are normalised to the shared internal event names ("payment_intent.succeeded", "payment_intent.payment_failed",
"refund.updated").
"""

import hashlib
import hmac
import json
from collections.abc import Mapping

import httpx

from app.core.config import get_settings
from app.core.payments.exceptions import (
    InvalidWebhookPayloadError,
    InvalidWebhookSignatureError,
    PaymentProviderError,
    PaymentProviderTimeoutError,
)
from app.core.payments.interface import (
    CredentialCheck,
    PaymentIntentResult,
    PaymentProvider,
    RefundResult,
    WebhookEvent,
)

BASE_URL = "https://api.razorpay.com/v1"


class RazorpayPaymentProvider(PaymentProvider):
    name = "razorpay"
    uses_event_headers = True
    supports_cancel = False

    def __init__(self, *, key_id: str, key_secret: str, webhook_secret: str | None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._auth = (key_id, key_secret)
        self._webhook_secret = webhook_secret
        self._transport = transport  # tests inject httpx.MockTransport; production leaves it None

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                base_url=BASE_URL, auth=self._auth, transport=self._transport,
                timeout=get_settings().stripe_timeout_seconds,  # one bounded wait for every provider call
            ) as client:
                return await client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise PaymentProviderTimeoutError() from exc
        except httpx.HTTPError as exc:
            raise PaymentProviderError("The payment provider is unreachable") from exc

    @staticmethod
    def _ok(response: httpx.Response, what: str) -> dict:
        if response.status_code >= 400:  # classified by status only: the body may echo request data
            raise PaymentProviderError(f"Razorpay rejected the {what} request ({response.status_code})")
        return response.json()

    async def validate_credentials(self) -> CredentialCheck:
        try:
            response = await self._request("GET", "/orders", params={"count": 1})
        except PaymentProviderError:
            return CredentialCheck(valid=False, reason="provider_unreachable")
        if response.status_code == 401:
            return CredentialCheck(valid=False, reason="authentication_failed")
        if response.status_code >= 400:
            return CredentialCheck(valid=False, reason="provider_unreachable_or_restricted")
        key_id = self._auth[0]
        mode = "test" if key_id.startswith("rzp_test_") else "live" if key_id.startswith("rzp_live_") else None
        return CredentialCheck(valid=True, detected_mode=mode)

    async def create_payment_intent(self, *, amount_cents: int, currency: str, metadata: dict, idempotency_key: str) -> PaymentIntentResult:
        body = {"amount": amount_cents, "currency": currency.upper(), "receipt": idempotency_key[:40],
                "notes": {k: str(v) for k, v in metadata.items()}}
        order = self._ok(await self._request("POST", "/orders", json=body), "create-order")
        return PaymentIntentResult(order["id"], order["id"], order.get("status", "created"))

    async def retrieve_payment_intent(self, *, provider_payment_id: str) -> PaymentIntentResult:
        order = self._ok(await self._request("GET", f"/orders/{provider_payment_id}"), "fetch-order")
        # Razorpay: created -> attempted -> paid. Mapped to the shared vocabulary (open states keep the order payable).
        status = {"paid": "succeeded", "created": "requires_payment_method", "attempted": "requires_action"}.get(order.get("status"), "unknown")
        return PaymentIntentResult(order["id"], order["id"], status)

    async def cancel_payment_intent(self, *, provider_payment_id: str, idempotency_key: str) -> PaymentIntentResult:
        raise PaymentProviderError("Razorpay orders cannot be cancelled")  # callers check supports_cancel first

    async def create_refund(self, *, provider_payment_id: str, amount_cents: int, currency: str, idempotency_key: str) -> RefundResult:
        # provider_payment_id here is the Razorpay PAYMENT id (pay_...): the service passes provider_reference for this provider.
        if not provider_payment_id.startswith("pay_"):
            raise PaymentProviderError("The Razorpay payment reference is not known yet, so a refund cannot be requested")
        response = await self._request(
            "POST", f"/payments/{provider_payment_id}/refund", json={"amount": amount_cents, "receipt": idempotency_key[:40]},
            headers={"X-Refund-Idempotency": idempotency_key},
        )
        refund = self._ok(response, "refund")
        status = {"processed": "succeeded", "pending": "pending", "failed": "failed"}.get(refund.get("status"), "pending")
        return RefundResult(refund["id"], refund["amount"], status, (refund.get("currency") or currency).lower())

    def verify_and_parse_webhook(self, *, payload: bytes, signature_header: str, headers: Mapping[str, str] | None = None) -> WebhookEvent:
        if not self._webhook_secret:
            raise InvalidWebhookSignatureError()
        expected = hmac.new(self._webhook_secret.encode(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, (signature_header or "").strip().lower()):
            raise InvalidWebhookSignatureError()
        try:
            body = json.loads(payload)
            event_name = body["event"]
            event_id = (headers or {}).get("x-razorpay-event-id") or hashlib.sha256(payload).hexdigest()
            return self._normalise(event_name, body["payload"], event_id)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise InvalidWebhookPayloadError() from exc

    @staticmethod
    def _normalise(event_name: str, payload: dict, event_id: str) -> WebhookEvent:
        if event_name in ("payment.captured", "payment.failed"):
            payment = payload["payment"]["entity"]
            captured = event_name == "payment.captured"
            return WebhookEvent(
                event_id=event_id,
                event_type="payment_intent.succeeded" if captured else "payment_intent.payment_failed",
                provider_payment_id=payment["order_id"], amount=payment["amount"],
                amount_received=payment["amount"] if captured else None,
                currency=str(payment["currency"]).lower(), provider="razorpay", provider_reference=payment["id"],
            )
        if event_name in ("refund.processed", "refund.failed"):
            refund = payload["refund"]["entity"]
            return WebhookEvent(
                event_id=event_id, event_type="refund.updated", provider_payment_id="", amount=refund["amount"],
                currency=str(refund.get("currency", "")).lower() or None, provider="razorpay",
                provider_refund_id=refund["id"], refund_status="succeeded" if event_name == "refund.processed" else "failed",
                provider_reference=refund["payment_id"],
            )
        # any other type is one we do not act on: recorded as `ignored`, never dropped
        return WebhookEvent(event_id=event_id, event_type=f"razorpay.{event_name}", provider_payment_id="", provider="razorpay")
