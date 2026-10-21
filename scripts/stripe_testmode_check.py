"""
STRIPE TEST-MODE verification (outbound API behavior). It talks to the real Stripe API with a TEST key only.

    docker compose exec -e STRIPE_TESTMODE_VERIFY=yes api python -m scripts.stripe_testmode_check --currency inr --amount 5000

Read docs/STRIPE_TESTMODE_VERIFICATION.md first. Safety rules, enforced before any network call:
  * the key must be a Stripe TEST key (sk_test_...): a live key (sk_live_/rk_live_) or anything else is refused;
  * STRIPE_TESTMODE_VERIFY=yes must be set explicitly (so it can never run by accident or in CI);
  * LIVE_PAYMENTS_ENABLED must be false.
The key is never printed. It creates a few TEST-mode PaymentIntents/refunds (tagged with a run id) in your Stripe TEST account.
Webhook behavior is verified with the Stripe CLI (see the runbook), not here.
"""

import argparse
import asyncio
import os
import sys
import uuid
from collections.abc import Mapping

TEST_CARD_PAYMENT_METHOD = "pm_card_visa"  # Stripe's documented test payment method


def guard_errors(env: Mapping[str, str]) -> list[str]:
    """Reasons this run must be refused (empty = allowed). Pure, so it is unit-tested without any network."""
    errors = []
    key = env.get("STRIPE_SECRET_KEY", "")
    if not key.startswith("sk_test_") or "REPLACE" in key:
        errors.append("STRIPE_SECRET_KEY must be a real Stripe TEST key (sk_test_...). Live keys are never accepted.")
    if env.get("STRIPE_TESTMODE_VERIFY", "").lower() != "yes":
        errors.append("Set STRIPE_TESTMODE_VERIFY=yes to confirm you want to call the Stripe TEST API.")
    if env.get("LIVE_PAYMENTS_ENABLED", "false").lower() in ("1", "true", "yes", "on"):
        errors.append("LIVE_PAYMENTS_ENABLED must be false.")
    return errors


class Report:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))

    @property
    def failed(self) -> int:
        return sum(1 for _, ok, _ in self.results if not ok)


async def run(currency: str, amount: int) -> int:
    import stripe

    from app.core.config import get_settings
    from app.core.money import get_currency, to_stripe_amount
    from app.core.payments.exceptions import PaymentProviderTimeoutError
    from app.core.payments.stripe_provider import StripePaymentProvider

    settings = get_settings()
    cur = get_currency(currency)
    expected = to_stripe_amount(amount, cur.code)  # the same conversion the application uses
    provider = StripePaymentProvider(secret_key=settings.stripe_secret_key, webhook_secret=settings.stripe_webhook_secret)
    run_id = uuid.uuid4().hex[:10]
    report = Report()
    print(f"run id {run_id}; currency {cur.code}; amount {expected} (minor units)")

    # 1. create: amount and currency arrive at Stripe exactly as the application computes them
    key = f"verify:{run_id}:create"
    intent = await provider.create_payment_intent(amount_cents=expected, currency=cur.code, metadata={"novacart_verify": run_id}, idempotency_key=key)
    remote = await asyncio.to_thread(stripe.PaymentIntent.retrieve, intent.provider_payment_id)
    report.check("create: Stripe holds the same amount and currency", remote.amount == expected and remote.currency == cur.code,
                 f"amount {remote.amount}, currency {remote.currency}")
    # 2. idempotent create: the same key returns the same intent (retries/concurrent requests cannot create two)
    again = await provider.create_payment_intent(amount_cents=expected, currency=cur.code, metadata={"novacart_verify": run_id}, idempotency_key=key)
    report.check("create: the same idempotency key returns the same intent", again.provider_payment_id == intent.provider_payment_id)
    # 3. cancel (what order expiry does), twice: the final state is returned, never an error
    first = await provider.cancel_payment_intent(provider_payment_id=intent.provider_payment_id, idempotency_key=f"verify:{run_id}:cancel")
    second = await provider.cancel_payment_intent(provider_payment_id=intent.provider_payment_id, idempotency_key=f"verify:{run_id}:cancel2")
    report.check("cancel: the intent is cancelled and cancelling again is safe", first.status == "canceled" and second.status == "canceled",
                 f"{first.status} / {second.status}")

    # 4. a payment that succeeds (test card), then a full refund
    paid = await asyncio.to_thread(
        stripe.PaymentIntent.create, amount=expected, currency=cur.code, payment_method=TEST_CARD_PAYMENT_METHOD, confirm=True,
        automatic_payment_methods={"enabled": True, "allow_redirects": "never"}, metadata={"novacart_verify": run_id},
        idempotency_key=f"verify:{run_id}:paid")
    report.check("payment: the test payment succeeded for the exact amount", paid.status == "succeeded" and paid.amount_received == expected,
                 f"{paid.status}, received {paid.amount_received}")
    refund_key = f"verify:{run_id}:refund"
    refund = await provider.create_refund(provider_payment_id=paid.id, amount_cents=expected, currency=cur.code, idempotency_key=refund_key)
    report.check("refund: a full refund is accepted in the original currency and amount",
                 refund.status in ("succeeded", "pending") and refund.amount_cents == expected and refund.currency in (None, cur.code),
                 f"{refund.status}, {refund.amount_cents} {refund.currency}")
    retry = await provider.create_refund(provider_payment_id=paid.id, amount_cents=expected, currency=cur.code, idempotency_key=refund_key)
    report.check("refund: retrying with the same key returns the SAME refund (no double refund)", retry.provider_refund_id == refund.provider_refund_id)
    try:  # a second, different refund of an already fully refunded payment must be refused by Stripe
        await provider.create_refund(provider_payment_id=paid.id, amount_cents=expected, currency=cur.code, idempotency_key=f"verify:{run_id}:refund2")
        report.check("refund: a second full refund is refused", False, "Stripe accepted it")
    except Exception as exc:  # noqa: BLE001 - any provider refusal is the expected outcome
        report.check("refund: a second full refund is refused", True, type(exc).__name__)

    # 5. a slow/unreachable provider is a bounded, explicit "unknown" outcome (not a hang, not a failure)
    object.__setattr__(settings, "stripe_timeout_seconds", 0.001)
    try:
        await provider.create_payment_intent(amount_cents=expected, currency=cur.code, metadata={}, idempotency_key=f"verify:{run_id}:timeout")
        report.check("timeout: an impossibly short timeout raises the explicit timeout error", False, "no error raised")
    except PaymentProviderTimeoutError:
        report.check("timeout: an impossibly short timeout raises the explicit timeout error", True)
    print(f"\n{len(report.results) - report.failed}/{len(report.results)} checks passed")
    return 1 if report.failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Stripe TEST-mode verification")
    parser.add_argument("--currency", default="inr")
    parser.add_argument("--amount", type=int, default=5000, help="amount in the currency's MINOR units")
    args = parser.parse_args()
    errors = guard_errors(os.environ)
    if errors:
        print("REFUSED:")
        for error in errors:
            print(f"  - {error}")
        return 2
    return asyncio.run(run(args.currency, args.amount))


if __name__ == "__main__":
    sys.exit(main())
