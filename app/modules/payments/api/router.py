from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.core.exceptions import NotFoundError
from app.core.payments.exceptions import InvalidWebhookSignatureError
from app.core.payments.registry import get_spec
from app.core.rate_limit import limiter, payment_limit
from app.modules.auth.dependencies.auth import AdminUser, OptionalUser
from app.modules.cart.dependencies.cart_identity import guest_payment_cookie_name
from app.modules.payments.dependencies.payment_deps import PaymentServiceDep
from app.modules.payments.schemas.payment import PaymentCreate, PaymentRead, RefundRead

payment_router = APIRouter(prefix="/payments", tags=["payments"])


@payment_router.post("", response_model=PaymentRead, status_code=status.HTTP_201_CREATED)
@limiter.limit(payment_limit)
async def create_payment(
    payload: PaymentCreate,
    request: Request,
    current_user: OptionalUser,
    service: PaymentServiceDep,
) -> PaymentRead:
    """
    Creates (or reuses) a Stripe PaymentIntent for one of the caller's own
    orders and returns the client secret needed to confirm it on the
    client side. Always self-service: this cannot be used to start a
    payment on another user's order, even by an admin.

    A signed-in user pays their own orders. A GUEST pays only with the order-scoped payment capability that checkout
    set in an HttpOnly cookie for that exact order (401 without any credential, 404 for a wrong or expired one).
    """
    guest_token = None
    if current_user is None:
        guest_token = request.cookies.get(guest_payment_cookie_name(payload.order_id))
        if not guest_token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated", headers={"WWW-Authenticate": "Bearer"}
            )
    payment, client_secret = await service.create_payment_for_order(
        order_id=payload.order_id,
        user_id=current_user.id if current_user is not None else None,
        guest_payment_token=guest_token,
    )
    return PaymentRead(
        id=payment.id,
        order_id=payment.order_id,
        status=payment.status,
        amount_cents=payment.amount_cents,
        currency=payment.currency,
        provider=payment.provider,
        client_secret=client_secret,
    )


@payment_router.post(
    "/{order_id}/refund", response_model=RefundRead, status_code=status.HTTP_202_ACCEPTED
)
async def request_refund(
    order_id: UUID,
    admin: AdminUser,
    service: PaymentServiceDep,
) -> RefundRead:
    """
    Admin-only. REQUESTS a refund of the order's successful payment; it does not refund anything yet.
    A background job asks the payment provider (with a retry-safe idempotency key), and the order is
    marked `refunded` only after the provider CONFIRMS the refund. Poll GET /admin/refunds (or the
    order) for the outcome. Asking again returns the same refund.
    """
    refund, _created = await service.request_refund(order_id=order_id, admin_user_id=admin.id)
    return RefundRead.model_validate(refund)


MAX_WEBHOOK_BYTES = 1_000_000  # real provider events are a few KB; anything this large is not one


async def _handle_webhook(request: Request, service: PaymentServiceDep, signature: str | None, provider: str | None = None) -> dict:
    """No auth: the provider calls this directly, authenticated ONLY by its signature over the exact raw bytes.
    The body must be read raw: parsing and re-serialising JSON first would break verification."""
    if not signature:
        raise InvalidWebhookSignatureError()
    payload = await request.body()
    if len(payload) > MAX_WEBHOOK_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Webhook payload too large")
    await service.handle_webhook(
        payload=payload, signature_header=signature, provider_name=provider, headers=dict(request.headers)
    )
    return {"status": "ok"}


@payment_router.post("/webhook", status_code=status.HTTP_200_OK)
async def stripe_webhook(
    request: Request,
    service: PaymentServiceDep,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
) -> dict:
    """Stripe's webhook (kept at this path for compatibility; same as /webhooks/stripe)."""
    return await _handle_webhook(request, service, stripe_signature)


@payment_router.post("/webhooks/{provider}", status_code=status.HTTP_200_OK)
async def provider_webhook(
    provider: str,
    request: Request,
    service: PaymentServiceDep,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
) -> dict:
    """One webhook URL per provider. Only `stripe` exists today; a new provider adds its own verification
    (inside its PaymentProvider) and signature header, never changes the shared lifecycle rules."""
    spec = get_spec(provider)
    if spec is None:
        raise NotFoundError("Unknown payment provider")
    # each provider signs with its OWN header; the URL alone never authenticates anything (the signature is verified against
    # that provider's stored secrets in PaymentService)
    return await _handle_webhook(request, service, request.headers.get(spec.signature_header) or stripe_signature, provider)
