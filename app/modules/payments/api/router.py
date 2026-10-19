from uuid import UUID

from fastapi import APIRouter, Header, Request, status

from app.modules.auth.dependencies.auth import AdminUser, CurrentUser
from app.modules.payments.dependencies.payment_deps import PaymentServiceDep
from app.modules.payments.schemas.payment import PaymentCreate, PaymentRead, RefundRead

payment_router = APIRouter(prefix="/payments", tags=["payments"])


@payment_router.post("", response_model=PaymentRead, status_code=status.HTTP_201_CREATED)
async def create_payment(
    payload: PaymentCreate,
    current_user: CurrentUser,
    service: PaymentServiceDep,
) -> PaymentRead:
    """
    Creates (or reuses) a Stripe PaymentIntent for one of the caller's own
    orders and returns the client secret needed to confirm it on the
    client side. Always self-service: this cannot be used to start a
    payment on another user's order, even by an admin.
    """
    payment, client_secret = await service.create_payment_for_order(
        order_id=payload.order_id, user_id=current_user.id
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


@payment_router.post("/webhook", status_code=status.HTTP_200_OK)
async def stripe_webhook(
    request: Request,
    service: PaymentServiceDep,
    stripe_signature: str = Header(..., alias="Stripe-Signature"),
) -> dict:
    """
    No auth -- Stripe calls this directly, authenticated only by the
    signature header. Must read the raw body: Stripe's signature is
    computed over the exact bytes sent, so parsing/re-serializing JSON
    first (as a Pydantic request body would) breaks verification.
    """
    payload = await request.body()
    await service.handle_webhook(payload=payload, signature_header=stripe_signature)
    return {"status": "ok"}
