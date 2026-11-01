"""Paying in DEMO mode with fake test details (customers' side). No real money, no real card details."""

from fastapi import APIRouter, Request

from app.core.database import DbSession
from app.core.rate_limit import limiter, payment_limit
from app.modules.auth.dependencies.auth import OptionalUser
from app.modules.cart.dependencies.cart_identity import guest_payment_cookie_name
from app.modules.orders.dependencies.order_deps import OrderServiceDep
from app.modules.payments.dependencies.payment_deps import PaymentServiceDep
from app.modules.payments.schemas.demo import DemoPayRequest, DemoPayResult, TestDetails
from app.modules.payments.services.demo_checkout import DemoCheckoutService, published_test_details

demo_router = APIRouter(prefix="/payments/demo", tags=["demo-payments"])


@demo_router.get("/test-details", response_model=TestDetails)
async def test_details(db: DbSession):
    """Public. The fake cards, UPI ids and banks the demo gateway accepts, and whether the demo gateway is the store's current gateway (so a
    storefront can show a clear "DEMO MODE" banner and the test details)."""
    from app.modules.payments.repositories.provider_config_repository import (
        ProviderConfigRepository,
    )

    preferred = await ProviderConfigRepository(db).get_preferred()
    return published_test_details(bool(preferred and preferred.provider == "demo" and preferred.status == "enabled"))


@demo_router.post("/pay", response_model=DemoPayResult)
@limiter.limit(payment_limit)
async def pay_with_test_details(
    payload: DemoPayRequest, request: Request, current_user: OptionalUser, payments: PaymentServiceDep, orders: OrderServiceDep, db: DbSession,
) -> DemoPayResult:
    """Completes a payment that was started (POST /payments) with the DEMO gateway, using one of the published fake test details. It goes through
    the same signed-event verification and order lifecycle as a live payment. A declined test card leaves the order unpaid, and you can try
    again. Anything that is not a listed test detail is refused (422 demo_only_test_details) without being recorded or echoed.
    A signed-in customer pays their own orders; a guest uses the order's payment cookie, exactly as for POST /payments."""
    guest_token = None if current_user is not None else request.cookies.get(guest_payment_cookie_name(payload.order_id))
    if current_user is None and not guest_token:
        from fastapi import HTTPException, status

        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    service = DemoCheckoutService(db, payments, orders)
    return await service.pay(payload, user_id=current_user.id if current_user is not None else None, guest_payment_token=guest_token)
