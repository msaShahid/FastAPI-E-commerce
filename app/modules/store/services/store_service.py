from app.core.money import get_currency
from app.modules.audit.services.audit_service import AuditService
from app.modules.auth.models.user import User
from app.modules.store.exceptions.store_exceptions import StoreCurrencyLockedError
from app.modules.store.repositories.store_repository import StoreRepository
from app.modules.store.schemas.store import StoreCurrencyRead


class StoreService:
    def __init__(self, repository: StoreRepository, audit: AuditService) -> None:
        self.repo = repository
        self.audit = audit

    async def read_currency(self) -> StoreCurrencyRead:
        code = await self.repo.get_currency()
        if code is None:
            return StoreCurrencyRead(currency=None, minor_unit_exponent=None, payment_verified=None)
        currency = get_currency(code)
        return StoreCurrencyRead(
            currency=currency.code, minor_unit_exponent=currency.exponent, payment_verified=currency.payment_verified
        )

    async def set_currency(self, actor: User, code: str) -> StoreCurrencyRead:
        """Sets the store currency. Allowed only while no order exists (the database enforces it too)."""
        currency = get_currency(code)  # raises 422 for an unsupported code
        current = await self.repo.get_currency()
        if current is not None and current != currency.code and await self.repo.any_orders():
            raise StoreCurrencyLockedError()
        await self.repo.set_currency(currency.code)
        await self.audit.log(
            actor_user_id=actor.id,
            action="store.currency_set",
            target_type="store_settings",
            target_id=1,
            details={"from": current, "to": currency.code},
        )
        return await self.read_currency()
