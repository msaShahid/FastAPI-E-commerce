"""A stand-in payment provider used ONLY by the demo seed. It never contacts Stripe or anyone else.

It lets the seed create payments, webhook-confirmed payments and refunds through the application's real code paths. Payment ids
look like `pi_demo_1` and refund ids like `re_demo_1`, so demo rows can never be mistaken for real provider records."""

import json

from app.core.payments.interface import (
    PaymentIntentResult,
    PaymentProvider,
    RefundResult,
    WebhookEvent,
)


class DemoProvider(PaymentProvider):
    name = "stripe"  # recorded on rows as the provider the app is configured for; the ids make the rows obviously demo

    def __init__(self) -> None:
        self.intents: dict[str, dict] = {}
        self.refunds: dict[str, dict] = {}
        self.last_payment_id: str | None = None

    async def create_payment_intent(self, *, amount_cents, currency, metadata, idempotency_key):
        for pid, intent in self.intents.items():
            if intent["key"] == idempotency_key:  # like a real provider: the same key returns the same intent
                self.last_payment_id = pid
                return PaymentIntentResult(pid, f"{pid}_secret_demo", "requires_payment_method")
        pid = f"pi_demo_{len(self.intents) + 1}"
        self.intents[pid] = {"amount": amount_cents, "currency": currency, "key": idempotency_key}
        self.last_payment_id = pid
        return PaymentIntentResult(pid, f"{pid}_secret_demo", "requires_payment_method")

    async def retrieve_payment_intent(self, *, provider_payment_id):
        return PaymentIntentResult(provider_payment_id, f"{provider_payment_id}_secret_demo", "requires_payment_method")

    async def cancel_payment_intent(self, *, provider_payment_id, idempotency_key):
        return PaymentIntentResult(provider_payment_id, f"{provider_payment_id}_secret_demo", "canceled")

    async def create_refund(self, *, provider_payment_id, amount_cents, currency, idempotency_key):
        entry = self.refunds.setdefault(idempotency_key, {"id": f"re_demo_{len(self.refunds) + 1}"})
        return RefundResult(entry["id"], amount_cents, "succeeded", currency)

    def verify_and_parse_webhook(self, *, payload, signature_header):
        data = json.loads(payload)
        intent = self.intents.get(data["pi"], {})
        return WebhookEvent(
            data["event_id"], data["type"], data["pi"], data.get("amount", intent.get("amount")), None,
            data.get("currency", intent.get("currency")), data.get("refund_id"), data.get("refund_status"),
        )
