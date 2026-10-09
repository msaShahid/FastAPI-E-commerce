from uuid import UUID

from fastapi import APIRouter, Header, Request, status

from app.modules.audit.dependencies.audit_deps import AuditServiceDep
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
    "/{order_id}/refund", response_model=RefundRead, status_code=status.HTTP_200_OK
)
async def refund_payment(
    order_id: UUID,
    admin: AdminUser,
    service: PaymentServiceDep,
    audit_service: AuditServiceDep,
) -> RefundRead:
    """
    Admin-only. Issues a real Stripe refund for the order's successful
    payment and moves the order to `refunded` only once Stripe confirms
    it -- see PaymentService.refund_payment.
    """
    payment, order = await service.refund_payment(
        order_id=order_id, admin_user_id=admin.id
    )

    await audit_service.log(
        actor_user_id=admin.id,
        action="payment.refunded",
        target_type="payment",
        target_id=payment.id,
        details={
            "order_id": str(order.id),
            "refunded_amount_cents": payment.refunded_amount_cents,
            "provider_refund_id": payment.provider_refund_id,
        },
    )

    return RefundRead(
        payment_id=payment.id,
        order_id=order.id,
        status=payment.status,
        refunded_amount_cents=payment.refunded_amount_cents,
        provider_refund_id=payment.provider_refund_id,
    )


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
