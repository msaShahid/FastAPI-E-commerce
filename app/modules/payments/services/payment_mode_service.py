"""The store's payment mode, controlled from the admin panel (no code change, no redeploy):

  demo  - the built-in simulator takes the payments (fake test details only; never real money; payments are marked as demo everywhere).
  test  - a real gateway (Stripe / Razorpay) in its sandbox, with the credentials an administrator entered.
  live  - a real gateway taking real money. OFF until an administrator has entered the credentials, validated them, had the gateway's signed
          webhook arrive, recorded the sandbox and production-review attestations, run the gateway check successfully AND explicitly
          activated live payments. Deactivating live payments leaves the store with NO provider (it never falls back to demo or anything else).
Nothing here handles a secret: credentials are only ever written (encrypted) and used by the provider adapters."""

from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.core.payments.registry import SPECS
from app.core.secrets import get_cipher
from app.modules.audit.services.audit_service import AuditService
from app.modules.auth.models.user import User
from app.modules.payments.exceptions.provider_exceptions import (
    LiveActivationBlockedError,
    LivePaymentsActiveError,
    ProviderConfigConflictError,
    ProviderConfigNotFoundError,
)
from app.modules.payments.models.provider_config import PaymentProviderConfig
from app.modules.payments.repositories.provider_config_repository import (
    PaymentSettingsRepository,
    ProviderConfigRepository,
)
from app.modules.payments.schemas.payment_mode import CheckItem, CheckResult
from app.modules.payments.services.provider_config_service import ProviderConfigService
from app.modules.payments.services.provider_resolver import build_provider
from app.modules.store.repositories.store_repository import StoreRepository

CHECK_MAX_AGE = timedelta(hours=24)       # a live gateway must have passed its check within a day of activation
CONFIRMATION = "ACTIVATE LIVE PAYMENTS"
# A test payment is created and immediately cancelled in the gateway's SANDBOX. Large enough to clear every gateway's minimum.
TEST_PAYMENT_MINOR_UNITS = 10000


def _now() -> datetime:
    return datetime.now(UTC)


class PaymentModeService:
    def __init__(self, db, audit: AuditService, *, cipher_factory=get_cipher) -> None:
        self.db = db
        self.audit = audit
        self.repo = ProviderConfigRepository(db)
        self.settings_repo = PaymentSettingsRepository(db)
        self.configs = ProviderConfigService(self.repo, audit, cipher_factory=cipher_factory)
        self._cipher_factory = cipher_factory

    # ------------------------------------------------------------------------------------------------ overview
    @staticmethod
    def _connection(config: PaymentProviderConfig) -> str:
        if config.credentials_status == "invalid":
            return "invalid"
        if config.credentials_status != "valid":
            return "not_validated"
        fully = (config.webhook_status == "verified" or config.provider == "demo") and config.last_check_ok is True
        return "connected" if fully else "credentials_ok"

    def _summary(self, config: PaymentProviderConfig) -> dict:
        view = self.configs.view(config)
        return {
            "id": config.id, "provider": config.provider, "label": view["label"], "mode": config.mode, "status": config.status,
            "is_preferred": config.is_preferred, "is_demo": config.provider == "demo", "connection": self._connection(config),
            "credentials_status": config.credentials_status, "webhook_status": config.webhook_status, "last_check_at": config.last_check_at,
            "last_check_ok": config.last_check_ok, "ready_for_use": view["readiness"]["ready_for_use"],
            "ready_for_live": view["readiness"]["ready_for_live"], "live_blockers": view["readiness"]["live_blockers"], "currencies": view["currencies"],
        }

    def _activation_blockers(self, config: PaymentProviderConfig) -> list[str]:
        blockers: list[str] = []
        if not get_settings().live_payments_enabled:
            blockers.append("live_payments_disabled_on_this_deployment")
        if config.mode != "live":
            blockers.append("not_a_live_configuration")
            return blockers
        if config.status != "enabled":
            blockers.append("gateway_not_enabled")
        _use, live = self.configs.readiness(config)
        blockers += [b for b in live if b not in blockers and b != "live_payments_disabled_on_this_deployment"]
        if config.last_check_at is None or config.last_check_ok is None:
            blockers.append("gateway_check_not_run")
        elif not config.last_check_ok:
            blockers.append("gateway_check_failed")
        elif _now() - config.last_check_at > CHECK_MAX_AGE:
            blockers.append("gateway_check_out_of_date")
        return blockers

    async def overview(self) -> dict:
        configs = await self.repo.list_all()
        settings = await self.settings_repo.get()
        preferred = next((c for c in configs if c.is_preferred), None)
        live_active = get_settings().live_payments_enabled and settings.live_payments_active
        open_ = bool(preferred and preferred.status == "enabled" and preferred.credentials_status == "valid"
                     and (preferred.mode != "live" or live_active) and not (preferred.provider == "demo" and live_active))
        mode = "none"
        if open_:
            mode = "demo" if preferred.provider == "demo" else ("live" if preferred.mode == "live" else "test")
        warnings: list[str] = []
        if preferred is not None and not open_:
            warnings.append("the selected gateway cannot take payments right now (it is not enabled, not validated, or live payments are not activated)")
        if mode == "none":
            warnings.append("customers cannot pay: no payment gateway is selected. Enable the demo gateway, or configure and select a real one")
        if mode == "demo":
            warnings.append("DEMO MODE: payments are simulated and no real money moves. Do not use the demo gateway for real sales")
        if mode == "test":
            warnings.append("TEST MODE: the gateway is in its sandbox and no real money moves")
        if not get_settings().live_payments_enabled:
            warnings.append("live payments are forbidden on this deployment (LIVE_PAYMENTS_ENABLED=false)")
        candidates = [c for c in configs if c.mode == "live"]
        best = min(candidates, key=lambda c: len(self._activation_blockers(c)), default=None)
        blockers = [] if best is None else self._activation_blockers(best)
        if best is None:
            blockers = ["no_live_gateway_configured"]
        messages = {"demo": "Demo mode: customers can test the whole payment flow with fake details; no real money moves.",
                    "test": "Test mode: a real gateway's sandbox is taking payments; no real money moves.",
                    "live": "Live mode: real payments are being taken.",
                    "none": "No payment gateway is selected: customers cannot pay yet."}
        catalog = [{"provider": s.name, "label": s.label, "modes": ["test"] if s.name == "demo" else ["test", "live"],
                    "credential_fields": list(s.credential_fields)} for s in SPECS.values()]
        existing = {c.provider for c in configs}
        return {
            "payment_mode": mode, "payments_open": open_, "message": messages[mode],
            "preferred": self._summary(preferred) if preferred else None,
            "live": {"deployment_allows_live": get_settings().live_payments_enabled, "active": live_active, "activated_at": settings.live_activated_at,
                     "activated_by_user_id": settings.live_activated_by_user_id, "config_id": settings.live_activated_config_id,
                     "can_activate": not blockers and not live_active, "activation_blockers": blockers if not live_active else []},
            "gateways": [self._summary(c) for c in configs],
            "to_set_up": [e for e in catalog if e["provider"] not in existing],
            "warnings": warnings,
        }

    # ------------------------------------------------------------------------------------------------ demo gateway
    async def enable_demo(self, admin: User) -> dict:
        if (await self.settings_repo.get()).live_payments_active:
            raise LivePaymentsActiveError("switch to the demo gateway")
        created = await self.configs.create_demo(admin)
        config = await self.repo.get(created["id"], for_update=True)
        if config.status != "enabled":
            await self.configs.enable(admin, config.id)
        await self.configs.prefer(admin, config.id)
        await self.audit.log(actor_user_id=admin.id, action="payments.demo_mode_enabled", target_type="payment_provider_config", target_id=config.id, details={})
        return await self.overview()

    async def disable_demo(self, admin: User) -> dict:
        config = await self.repo.get_by_provider_mode("demo", "test")
        if config is None:
            raise ProviderConfigNotFoundError()
        await self.configs.disable(admin, config.id)
        await self.audit.log(actor_user_id=admin.id, action="payments.demo_mode_disabled", target_type="payment_provider_config", target_id=config.id, details={})
        return await self.overview()

    # ------------------------------------------------------------------------------------------------ live activation
    async def activate_live(self, admin: User, config_id, confirmation: str, note: str | None) -> dict:
        if confirmation.strip() != CONFIRMATION:
            raise LiveActivationBlockedError(["confirmation_text_incorrect"])
        settings = await self.settings_repo.get(for_update=True)
        config = await self.repo.get(config_id, for_update=True)
        if config is None:
            raise ProviderConfigNotFoundError()
        blockers = self._activation_blockers(config)
        if blockers:
            await self.audit.log(actor_user_id=admin.id, action="payments.live_activation_refused", target_type="payment_provider_config",
                                 target_id=config.id, details={"blockers": blockers})
            await self.db.commit()  # the refusal itself is on record
            raise LiveActivationBlockedError(blockers)
        settings.live_payments_active, settings.live_activated_at = True, _now()
        settings.live_activated_by_user_id, settings.live_activated_config_id, settings.note = admin.id, config.id, note
        await self.db.flush()
        await self.configs.prefer(admin, config.id)  # the live gateway now takes new payments; the demo gateway no longer can
        await self.audit.log(actor_user_id=admin.id, action="payments.live_activated", target_type="payment_provider_config", target_id=config.id,
                             details={"provider": config.provider, "note": note})
        return await self.overview()

    async def deactivate_live(self, admin: User, reason: str) -> dict:
        settings = await self.settings_repo.get(for_update=True)
        if not settings.live_payments_active:
            raise ProviderConfigConflictError("live_payments_not_active", "Live payments are not active")
        settings.live_payments_active, settings.last_deactivated_at = False, _now()
        await self.db.flush()
        for config in await self.repo.list_all():  # nothing takes over: no gateway is preferred until an administrator chooses one
            if config.is_preferred:
                config.is_preferred = False
        await self.db.flush()
        await self.audit.log(actor_user_id=admin.id, action="payments.live_deactivated", target_type="payment_settings", target_id=1, details={"reason": reason})
        return await self.overview()

    # ------------------------------------------------------------------------------------------------ test and verify a gateway
    async def run_checks(self, admin: User, config_id) -> dict:
        config = await self.repo.get(config_id)
        if config is None:
            raise ProviderConfigNotFoundError()
        spec = SPECS[config.provider]
        live = config.mode == "live"
        checks: list[CheckItem] = []

        def add(check: str, status: str, required: bool, message: str) -> None:
            checks.append(CheckItem(check=check, status=status, required=required, message=message))

        # 1. credentials + 2. mode (the existing validation: a harmless authenticated call, and the key's own mode)
        validation = await self.configs.validate(admin, config_id)
        config = await self.repo.get(config_id)
        if validation["valid"]:
            add("credentials", "pass", True, "The gateway accepted the stored credentials")
        else:
            add("credentials", "fail", True, f"The credentials were not accepted ({validation['reason'] or 'invalid'}): enter them again")
        detected = validation["detected_mode"]
        if detected is None:
            add("mode_matches", "warn", False, "The gateway did not say whether these credentials are test or live ones")
        elif detected == config.mode:
            add("mode_matches", "pass", True, f"The credentials are {detected}-mode credentials, as configured")
        else:
            add("mode_matches", "fail", True, f"These are {detected}-mode credentials but this configuration is {config.mode}")
        # 3. currencies
        store_currency, _ = await StoreRepository(self.db).effective_currency(get_settings().default_store_currency)
        enabled = set(config.currencies) & spec.currencies
        if store_currency and store_currency in enabled:
            add("currencies", "pass", True, f"{store_currency.upper()} (the store currency) is enabled for this gateway")
        else:
            add("currencies", "fail", True, "The store currency is not enabled for this gateway: set the gateway's currencies")
        # 4. webhook
        if config.webhook_secret_ciphertext:
            add("webhook_secret", "pass", True, "A webhook signing secret is stored")
        else:
            add("webhook_secret", "fail", True, "No webhook signing secret is stored: add the secret from the gateway's dashboard")
        url = f"/api/v1/payments/webhooks/{config.provider}"
        if config.webhook_status == "verified":
            add("webhook_delivery", "pass", live, "A correctly signed event from the gateway has been received")
        else:
            add("webhook_delivery", "warn" if not live else "fail", live,
                f"No signed event has arrived yet. Send a test event from the gateway's dashboard to {url} (events: {', '.join(spec.webhook_events)})")
        # 5. a test payment in the SANDBOX (never in live mode)
        if live:
            add("test_payment", "skipped", False, "Live checks are read-only: no payment is created on a live gateway")
        elif not validation["valid"]:
            add("test_payment", "skipped", True, "Skipped: the credentials must be accepted first")
        else:
            await self._test_payment(config, spec, add)
        # 6. live attestations
        if live:
            add("sandbox_verification", "pass" if config.sandbox_verified_at else "fail", True,
                "Sandbox verification is recorded" if config.sandbox_verified_at else "Record the sandbox verification (POST .../attestations)")
            add("production_review", "pass" if config.production_review_at else "fail", True,
                "A production review is recorded" if config.production_review_at else "Record the production review (POST .../attestations)")
        ok = all(c.status == "pass" for c in checks if c.required)
        result = CheckResult(ok=ok, checked_at=_now(), checks=checks)
        config = await self.repo.get(config_id, for_update=True)
        config.last_check_at, config.last_check_ok = result.checked_at, ok
        config.last_check_result = {"checks": [c.model_dump() for c in checks]}
        await self.repo.save(config)
        await self.audit.log(actor_user_id=admin.id, action="provider_config.checked", target_type="payment_provider_config", target_id=config.id,
                             details={"ok": ok, "failed": [c.check for c in checks if c.required and c.status != "pass"], "mode": config.mode})
        return result.model_dump()

    async def _test_payment(self, config: PaymentProviderConfig, spec, add) -> None:
        """Creates (and, where the gateway allows, cancels) one payment in the gateway's SANDBOX: proof that the whole call path works."""
        from app.core.payments.exceptions import PaymentProviderError

        adapter = build_provider(config, self._cipher_factory(), db=self.db)
        currency = next(iter(sorted(set(config.currencies) & spec.currencies)), None)
        if currency is None:
            add("test_payment", "skipped", True, "Skipped: no currency is enabled")
            return
        key = f"gateway-check:{config.id}:{int(_now().timestamp())}"
        await self.db.commit()  # no transaction is open while we wait for the gateway
        try:
            intent = await adapter.create_payment_intent(amount_cents=TEST_PAYMENT_MINOR_UNITS, currency=currency, metadata={"purpose": "gateway_check"}, idempotency_key=key)
            if spec.supports_cancel:
                await adapter.cancel_payment_intent(provider_payment_id=intent.provider_payment_id, idempotency_key=key + ":cancel")
                add("test_payment", "pass", True, "A test payment was created and cancelled in the gateway's sandbox")
            else:
                add("test_payment", "pass", True, "A test order was created in the gateway's sandbox (this gateway cannot cancel it; it expires unpaid)")
        except PaymentProviderError:
            add("test_payment", "fail", True, "The gateway did not accept a test payment: check the credentials, the currency and the gateway's status")
        except Exception:  # noqa: BLE001 - a failed check must be reported, never crash the screen; the detail stays out of the response
            add("test_payment", "fail", True, "The test payment failed for an unexpected reason")
