"""
The providers NovaCart knows how to talk to, and what each one is VERIFIED to support. Capabilities are explicit: a provider is
never assumed to support a currency, cancellation, or an event just because another one does.

`verification` says how far a provider has been verified, honestly:
  stripe   : signature verification and event parsing verified with REAL signatures; outbound API calls NOT verified against Stripe.
  razorpay : adapter tested against MOCKED HTTP only; not verified against Razorpay's sandbox or official documentation.
Neither is production-ready until sandbox verification and a production review are recorded for the configuration.
"""

from collections.abc import Callable
from dataclasses import dataclass

from app.core.money import CURRENCIES
from app.core.payments.interface import PaymentProvider
from app.core.payments.razorpay_provider import RazorpayPaymentProvider
from app.core.payments.stripe_provider import StripePaymentProvider

MODES = ("test", "live")


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    label: str
    currencies: frozenset[str]             # currencies the PLATFORM has verified for this provider (an admin may enable a subset)
    supports_cancel: bool
    supports_full_refund: bool
    refund_needs_provider_reference: bool  # the refund call needs the provider's PAYMENT id (not the order/intent id)
    dispute_events: bool
    signature_header: str                  # the (lower-case) request header carrying the webhook signature
    webhook_events: tuple[str, ...]        # what to subscribe to at the provider
    credential_fields: tuple[str, ...]     # secret fields the admin supplies (values are never returned)
    verification: str
    factory: Callable[[dict, str | None], PaymentProvider]  # (credentials, webhook_secret) -> adapter


def _stripe(credentials: dict, webhook_secret: str | None) -> PaymentProvider:
    return StripePaymentProvider(secret_key=credentials["secret_key"], webhook_secret=webhook_secret or "")


def _razorpay(credentials: dict, webhook_secret: str | None) -> PaymentProvider:
    return RazorpayPaymentProvider(
        key_id=credentials["key_id"], key_secret=credentials["key_secret"], webhook_secret=webhook_secret
    )


SPECS: dict[str, ProviderSpec] = {
    "stripe": ProviderSpec(
        name="stripe", label="Stripe", currencies=frozenset(c.code for c in CURRENCIES.values() if c.payment_verified),
        supports_cancel=True, supports_full_refund=True, refund_needs_provider_reference=False, dispute_events=True,
        webhook_events=("payment_intent.succeeded", "payment_intent.payment_failed", "refund.updated", "refund.created",
                        "charge.dispute.created", "charge.dispute.updated", "charge.dispute.closed"),
        credential_fields=("secret_key",), signature_header="stripe-signature",
        verification="signature verification and parsing verified with real signatures; outbound API calls not verified against Stripe",
        factory=_stripe,
    ),
    "razorpay": ProviderSpec(
        name="razorpay", label="Razorpay", currencies=frozenset({"inr"}),  # INR only until others are verified for this provider
        supports_cancel=False, supports_full_refund=True, refund_needs_provider_reference=True, dispute_events=False,
        webhook_events=("payment.captured", "payment.failed", "refund.processed", "refund.failed"),
        credential_fields=("key_id", "key_secret"), signature_header="x-razorpay-signature",
        verification="adapter tested with mocked HTTP only; not verified against Razorpay's sandbox or official documentation",
        factory=_razorpay,
    ),
}


def get_spec(provider: str) -> ProviderSpec | None:
    return SPECS.get(provider)
