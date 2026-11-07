"""The administrator's payment controls: the store's payment mode, the demo gateway, activating and deactivating live payments."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.database import DbSession
from app.modules.audit.dependencies.audit_deps import AuditServiceDep
from app.modules.payments.schemas.payment_mode import (
    DemoEnable,
    LiveActivate,
    LiveDeactivate,
    PaymentOverview,
)
from app.modules.payments.services.payment_mode_service import PaymentModeService
from app.modules.roles.dependencies.permission_deps import (
    PaymentsLive,
    PaymentsManage,
    PaymentsRead,
)

payment_mode_router = APIRouter(prefix="/admin/payments", tags=["payment-modes"])


def get_payment_mode_service(db: DbSession, audit: AuditServiceDep) -> PaymentModeService:
    return PaymentModeService(db, audit)


ServiceDep = Annotated[PaymentModeService, Depends(get_payment_mode_service)]


@payment_mode_router.get("/overview", response_model=PaymentOverview)
async def payments_overview(_admin: PaymentsRead, service: ServiceDep):
    """Everything about how this store takes payments, in one call: the current mode (`demo`, `test`, `live` or `none`), whether customers can
    pay right now, each gateway's connection status, whether live payments can be activated and what still blocks it."""
    return await service.overview()


@payment_mode_router.post("/demo/enable", response_model=PaymentOverview)
async def enable_demo_mode(payload: DemoEnable, admin: PaymentsManage, service: ServiceDep):
    """Switch the store to DEMO mode: the built-in gateway takes payments with fake test details and no real money moves. Needs no credentials.
    Refused while live payments are active."""
    from app.modules.payments.exceptions.provider_exceptions import ProviderConfigInvalidError

    if not payload.confirm_no_real_money:
        raise ProviderConfigInvalidError("confirmation_required", "Confirm that demo payments move no real money (confirm_no_real_money: true)")
    return await service.enable_demo(admin)


@payment_mode_router.post("/demo/disable", response_model=PaymentOverview)
async def disable_demo_mode(admin: PaymentsManage, service: ServiceDep):
    """Stop the demo gateway taking new payments. The store is left with NO payment gateway until you select one."""
    return await service.disable_demo(admin)


@payment_mode_router.post("/live/activate", response_model=PaymentOverview)
async def activate_live_payments(payload: LiveActivate, admin: PaymentsLive, service: ServiceDep):
    """The explicit approval to take REAL payments. Refused (409 live_activation_blocked, listing why) unless the chosen live gateway has valid
    credentials, a verified webhook, the sandbox and production-review attestations and a passing gateway check no older than a day, and
    `confirmation` is exactly "ACTIVATE LIVE PAYMENTS". It then becomes the store's payment gateway."""
    return await service.activate_live(admin, payload.config_id, payload.confirmation, payload.note)


@payment_mode_router.post("/live/deactivate", response_model=PaymentOverview)
async def deactivate_live_payments(payload: LiveDeactivate, admin: PaymentsLive, service: ServiceDep):
    """Stop taking real payments at once. The store is left with NO gateway (nothing, in particular not the demo gateway, takes over).
    Payments already made keep being reconciled and refunded."""
    return await service.deactivate_live(admin, payload.reason)
