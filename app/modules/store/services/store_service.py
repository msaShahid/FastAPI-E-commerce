from app.core.config import get_settings
from app.core.money import get_currency
from app.modules.audit.services.audit_service import AuditService
from app.modules.auth.models.user import User
from app.modules.store.exceptions.store_exceptions import StoreCurrencyLockedError
from app.modules.store.repositories.store_repository import StoreRepository
from app.modules.store.schemas.store import StoreCurrencyRead, StoreSettingsRead


class StoreService:
    def __init__(self, repository: StoreRepository, audit: AuditService) -> None:
        self.repo = repository
        self.audit = audit

    async def read_currency(self) -> StoreCurrencyRead:
        settings = get_settings()
        code, configured = await self.repo.effective_currency(settings.default_store_currency)
        if code is None:
            return StoreCurrencyRead(currency=None, minor_unit_exponent=None, payment_verified=None)
        currency = get_currency(code)
        warnings = []
        if not currency.payment_verified:
            warnings.append(f"Payments in {currency.code} are not verified for the payment integration")
        elif currency.code not in settings.enabled_payment_currencies():
            warnings.append(f"{currency.code} is not in PAYMENT_CURRENCIES_ENABLED, so payments are refused")
        return StoreCurrencyRead(
            currency=currency.code, minor_unit_exponent=currency.exponent, payment_verified=currency.payment_verified,
            configured=configured, warnings=warnings,
        )

    async def read_public_settings(self) -> StoreSettingsRead:
        """What a storefront may know about how buying works here, and why buying might currently be impossible."""
        settings = get_settings()
        currency = await self.read_currency()
        reasons: list[str] = []
        if currency.currency is None:
            reasons.append("The store currency is not set")
        if settings.flat_shipping_cents is None:
            reasons.append("Shipping is not configured")
        if settings.tax_rate_percent is None:
            reasons.append("Tax is not configured")
        payments_ok = currency.currency is not None and not currency.warnings
        if currency.currency is not None and not payments_ok:
            reasons.append("Payments are not enabled for the store currency")
        return StoreSettingsRead(
            currency=currency.currency,
            minor_unit_exponent=currency.minor_unit_exponent,
            flat_shipping_cents=settings.flat_shipping_cents,
            free_shipping_threshold_cents=settings.free_shipping_threshold_cents or None,
            pending_order_timeout_minutes=settings.pending_order_timeout_minutes,
            checkout_available=currency.currency is not None and settings.flat_shipping_cents is not None
            and settings.tax_rate_percent is not None,
            payments_available=payments_ok,
            unavailable_reasons=reasons,
        )

    async def set_currency(self, actor: User, code: str) -> StoreCurrencyRead:
        """Initialises the store currency, ONCE. After that it is fixed for the life of the store: repeating the same value is a
        harmless no-op, anything else is refused (the database refuses it too, even if this check is bypassed)."""
        currency = get_currency(code)  # raises 422 for an unsupported code
        current = await self.repo.get_currency()
        if current is not None:
            if current == currency.code:
                return await self.read_currency()
            raise StoreCurrencyLockedError()
        if not await self.repo.initialize_currency(currency.code):
            # A concurrent request (or the first checkout, which records the default) initialised it between our read and write.
            if await self.repo.get_currency() != currency.code:
                raise StoreCurrencyLockedError()
            return await self.read_currency()
        await self.audit.log(
            actor_user_id=actor.id,
            action="store.currency_set",
            target_type="store_settings",
            target_id=1,
            details={"from": None, "to": currency.code},
        )
        return await self.read_currency()
