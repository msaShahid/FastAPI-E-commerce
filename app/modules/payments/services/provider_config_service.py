"""
Administration of payment provider configurations. Rules enforced here (and re-checked by database constraints):

  * credentials are validated for shape and MODE (a live key cannot be saved as a test configuration), then stored encrypted;
  * a configuration can be ENABLED only when its credentials were validated against the provider and it has currencies; a LIVE one
    additionally needs LIVE_PAYMENTS_ENABLED, a webhook that has actually delivered a correctly signed event, and recorded
    sandbox-verification and production-review attestations. Entering credentials NEVER enables anything;
  * rotating credentials pauses new payments on that configuration (disabled, un-preferred) until it is validated and enabled
    again; existing payments keep reconciling through it;
  * disabling blocks NEW payments only; a configuration is never deleted;
  * every change is audited (who, what, outcome) and no audit detail ever contains a secret.
"""

import json
from datetime import UTC, datetime
from uuid import UUID

from app.core.config import get_settings
from app.core.money import get_currency
from app.core.payments.registry import MODES, SPECS, ProviderSpec, get_spec
from app.core.secrets import SecretCipher, get_cipher, hint
from app.modules.audit.services.audit_service import AuditService
from app.modules.auth.models.user import User
from app.modules.payments.exceptions.provider_exceptions import (
    ProviderConfigConflictError,
    ProviderConfigInvalidError,
    ProviderConfigNotFoundError,
    ProviderNotReadyError,
)
from app.modules.payments.models.provider_config import PaymentProviderConfig
from app.modules.payments.repositories.provider_config_repository import ProviderConfigRepository
from app.modules.payments.services.provider_resolver import build_provider
from app.modules.store.repositories.store_repository import StoreRepository

CREDENTIAL_PREFIXES = {
    "stripe": {"secret_key": {"test": ("sk_test_", "rk_test_"), "live": ("sk_live_", "rk_live_")}},
    "razorpay": {"key_id": {"test": ("rzp_test_",), "live": ("rzp_live_",)}},
}
SECRET_FIELD = {"stripe": "secret_key", "razorpay": "key_secret"}
PUBLIC_FIELD = {"razorpay": "key_id"}


def _now() -> datetime:
    return datetime.now(UTC)


class ProviderConfigService:
    def __init__(self, repo: ProviderConfigRepository, audit: AuditService, *, cipher_factory=get_cipher) -> None:
        self.repo = repo
        self.audit = audit
        self._cipher_factory = cipher_factory

    # ------------------------------------------------------------ views (sanitized)
    @staticmethod
    def spec_of(config: PaymentProviderConfig) -> ProviderSpec:
        return SPECS[config.provider]

    def readiness(self, config: PaymentProviderConfig) -> tuple[list[str], list[str]]:
        """(blockers for use, blockers for live). Safe codes only."""
        spec = self.spec_of(config)
        blockers = []
        if not config.credentials_ciphertext:
            blockers.append("credentials_not_set")
        elif config.credentials_status == "unvalidated":
            blockers.append("credentials_not_validated")
        elif config.credentials_status == "invalid":
            blockers.append("credentials_invalid")
        if not set(config.currencies) & spec.currencies:
            blockers.append("no_supported_currency_enabled")
        live = []
        if config.mode != "live":
            live.append("not_a_live_configuration")
        else:
            if not get_settings().live_payments_enabled:
                live.append("live_payments_disabled_on_this_deployment")
            if not config.webhook_secret_ciphertext:
                live.append("webhook_secret_not_set")
            elif config.webhook_status != "verified":
                live.append("webhook_not_verified")
            if config.sandbox_verified_at is None:
                live.append("sandbox_verification_not_recorded")
            if config.production_review_at is None:
                live.append("production_review_not_recorded")
        return blockers, blockers + [b for b in live if b not in blockers]

    def view(self, config: PaymentProviderConfig) -> dict:
        spec = self.spec_of(config)
        blockers, live_blockers = self.readiness(config)
        return {
            "id": config.id, "provider": config.provider, "label": spec.label, "mode": config.mode, "status": config.status,
            "is_preferred": config.is_preferred, "currencies": sorted(config.currencies),
            "credentials": {"configured": bool(config.credentials_ciphertext), "hint": config.credential_hint,
                            "status": config.credentials_status, "validated_at": config.credentials_validated_at,
                            "public_identifier": config.public_identifier},
            "webhook": {"secret_configured": bool(config.webhook_secret_ciphertext), "hint": config.webhook_hint,
                        "previous_secret_active": bool(config.previous_webhook_secret_ciphertext),
                        "status": config.webhook_status, "verified_at": config.webhook_verified_at,
                        "url": f"/api/v1/payments/webhooks/{config.provider}", "events": list(spec.webhook_events)},
            "capabilities": self.capabilities(spec),
            "readiness": {"ready_for_use": not blockers and config.status == "enabled", "ready_for_live": not live_blockers,
                          "blockers": blockers, "live_blockers": live_blockers},
            "sandbox_verified_at": config.sandbox_verified_at, "production_review_at": config.production_review_at,
            "version": config.version, "created_by_user_id": config.created_by_user_id,
            "updated_by_user_id": config.updated_by_user_id, "created_at": config.created_at, "updated_at": config.updated_at,
        }

    @staticmethod
    def capabilities(spec: ProviderSpec) -> dict:
        return {"currencies": sorted(spec.currencies), "supports_cancel": spec.supports_cancel,
                "supports_full_refund": spec.supports_full_refund, "dispute_events": spec.dispute_events,
                "refund_needs_provider_reference": spec.refund_needs_provider_reference, "verification": spec.verification}

    def catalog(self) -> list[dict]:
        return [{"provider": s.name, "label": s.label, "modes": list(MODES), "credential_fields": list(s.credential_fields),
                 "capabilities": self.capabilities(s), "webhook_events": list(s.webhook_events)} for s in SPECS.values()]

    async def list_configs(self) -> list[dict]:
        return [self.view(c) for c in await self.repo.list_all()]

    async def _get(self, config_id: UUID, *, lock: bool = False) -> PaymentProviderConfig:
        config = await self.repo.get(config_id, for_update=lock)
        if config is None:
            raise ProviderConfigNotFoundError()
        return config

    async def get_view(self, config_id: UUID) -> dict:
        return self.view(await self._get(config_id))

    async def _audit(self, admin: User, action: str, config: PaymentProviderConfig, **details) -> None:
        await self.audit.log(actor_user_id=admin.id, action=f"provider_config.{action}", target_type="payment_provider_config",
                             target_id=config.id, details={"provider": config.provider, "mode": config.mode, **details})

    # ------------------------------------------------------------ input handling
    @staticmethod
    def _secret(value) -> str | None:
        return None if value is None else value.get_secret_value().strip() or None

    def parse_credentials(self, spec: ProviderSpec, mode: str, raw) -> tuple[dict, str | None, str]:
        """(credentials, public identifier, hint). Checks shape and that the key belongs to the declared MODE."""
        values = {name: self._secret(getattr(raw, name)) for name in spec.credential_fields}
        extra = [n for n in ("secret_key", "key_id", "key_secret") if n not in spec.credential_fields and self._secret(getattr(raw, n))]
        missing = [n for n, v in values.items() if not v]
        if missing or extra:
            raise ProviderConfigInvalidError(
                "credentials_fields_invalid", f"{spec.label} needs exactly these credential fields: {', '.join(spec.credential_fields)}")
        for field, by_mode in CREDENTIAL_PREFIXES[spec.name].items():
            if not values[field].startswith(by_mode[mode]):
                raise ProviderConfigInvalidError(
                    "credentials_mode_mismatch", f"The {field} does not look like a {mode}-mode {spec.label} credential")
        return values, values.get(PUBLIC_FIELD.get(spec.name, "")), hint(values[SECRET_FIELD[spec.name]])

    def parse_webhook_secret(self, spec: ProviderSpec, value) -> tuple[str, str]:
        secret = self._secret(value)
        if not secret or len(secret) < 8 or (spec.name == "stripe" and not secret.startswith("whsec_")):
            raise ProviderConfigInvalidError("webhook_secret_invalid", f"That does not look like a {spec.label} webhook signing secret")
        return secret, hint(secret)

    def _check_currencies(self, spec: ProviderSpec, currencies: list[str]) -> list[str]:
        out = []
        for code in currencies:
            normalized = get_currency(code).code  # 422 for a code the platform does not know
            if normalized not in spec.currencies:
                raise ProviderConfigInvalidError(
                    "currency_not_supported_by_provider", f"{spec.label} is not verified for {normalized.upper()} on this platform")
            out.append(normalized)
        return sorted(set(out))

    # ------------------------------------------------------------ operations
    async def create(self, admin: User, *, provider: str, mode: str, credentials, webhook_secret, currencies: list[str] | None) -> dict:
        spec = get_spec(provider.strip().lower())
        if spec is None:
            raise ProviderConfigInvalidError("provider_not_supported", f"Unsupported payment provider. Supported: {', '.join(SPECS)}")
        if await self.repo.get_by_provider_mode(spec.name, mode) is not None:
            raise ProviderConfigConflictError("provider_config_exists", f"{spec.label} ({mode}) is already configured: rotate its credentials instead")
        parsed, public_id, credential_hint = self.parse_credentials(spec, mode, credentials)
        cipher: SecretCipher = self._cipher_factory()  # 503 if storage is not configured: never a plaintext fallback
        webhook_blob = webhook_hint = None
        if webhook_secret is not None:
            secret, webhook_hint = self.parse_webhook_secret(spec, webhook_secret)
            webhook_blob = cipher.encrypt(secret)
        if currencies is None:
            store_currency, _ = await StoreRepository(self.repo.db).effective_currency(get_settings().default_store_currency)
            currencies = [store_currency] if store_currency in spec.currencies else []
        config = await self.repo.create(
            provider=spec.name, mode=mode, status="draft", credentials_ciphertext=cipher.encrypt(json.dumps(parsed)),
            credential_hint=credential_hint, public_identifier=public_id, webhook_secret_ciphertext=webhook_blob,
            webhook_hint=webhook_hint, currencies=self._check_currencies(spec, currencies),
            created_by_user_id=admin.id, updated_by_user_id=admin.id,
        )
        await self._audit(admin, "created", config, currencies=config.currencies, webhook_secret_set=webhook_blob is not None)
        return self.view(config)

    async def rotate(self, admin: User, config_id: UUID, *, credentials, webhook_secret) -> dict:
        config = await self._get(config_id, lock=True)
        spec = self.spec_of(config)
        if credentials is None and webhook_secret is None:
            raise ProviderConfigInvalidError("nothing_to_rotate", "Provide new credentials and/or a new webhook secret")
        cipher = self._cipher_factory()
        changed = []
        if credentials is not None:
            parsed, public_id, credential_hint = self.parse_credentials(spec, config.mode, credentials)
            config.credentials_ciphertext = cipher.encrypt(json.dumps(parsed))
            config.credential_hint, config.public_identifier = credential_hint, public_id
            config.credentials_status, config.credentials_validated_at = "unvalidated", None
            # evidence gathered for the OLD credentials does not carry over to new ones
            config.sandbox_verified_at = config.sandbox_verification_ref = None
            config.production_review_at = config.production_review_ref = None
            changed.append("credentials")
        if webhook_secret is not None:
            secret, webhook_hint = self.parse_webhook_secret(spec, webhook_secret)
            # the old secret keeps VERIFYING events during the overlap, so deliveries already in flight are not lost
            config.previous_webhook_secret_ciphertext = config.webhook_secret_ciphertext
            config.webhook_secret_ciphertext, config.webhook_hint = cipher.encrypt(secret), webhook_hint
            config.webhook_status, config.webhook_verified_at = "unverified", None
            changed.append("webhook_secret")
        paused = config.status == "enabled"
        if paused:  # new payments pause until it is validated and enabled again; existing payments keep reconciling
            config.status, config.is_preferred = "disabled", False
        config.version += 1
        config.updated_by_user_id = admin.id
        await self.repo.save(config)
        await self._audit(admin, "rotated", config, changed=changed, version=config.version, new_payments_paused=paused)
        return self.view(config)

    async def retire_previous_webhook_secret(self, admin: User, config_id: UUID) -> dict:
        config = await self._get(config_id, lock=True)
        config.previous_webhook_secret_ciphertext = None
        config.updated_by_user_id = admin.id
        await self.repo.save(config)
        await self._audit(admin, "previous_webhook_secret_retired", config)
        return self.view(config)

    async def validate(self, admin: User, config_id: UUID) -> dict:
        config = await self._get(config_id)
        adapter = build_provider(config, self._cipher_factory())
        await self.repo.db.commit()  # no transaction is open while we wait for the provider
        check = await adapter.validate_credentials()
        config = await self._get(config_id, lock=True)
        valid = check.valid and (check.detected_mode in (None, config.mode))
        reason = check.reason or (None if valid else "mode_mismatch")
        config.credentials_status = "valid" if valid else "invalid"
        config.credentials_validated_at = _now() if valid else None
        config.updated_by_user_id = admin.id
        if not valid and config.status == "enabled":
            config.status, config.is_preferred = "disabled", False  # never leave an enabled configuration with bad credentials
        await self.repo.save(config)
        await self._audit(admin, "credentials_validated", config, valid=valid, reason=reason)
        return {"valid": valid, "reason": reason, "detected_mode": check.detected_mode, "config": self.view(config)}

    async def set_currencies(self, admin: User, config_id: UUID, currencies: list[str]) -> dict:
        config = await self._get(config_id, lock=True)
        spec = self.spec_of(config)
        normalized = self._check_currencies(spec, currencies)
        if config.status == "enabled" and not normalized:
            raise ProviderConfigConflictError("currencies_required", "An enabled provider needs at least one currency: disable it first")
        config.currencies, config.updated_by_user_id = normalized, admin.id
        await self.repo.save(config)
        await self._audit(admin, "currencies_set", config, currencies=normalized)
        return self.view(config)

    async def enable(self, admin: User, config_id: UUID) -> dict:
        config = await self._get(config_id, lock=True)
        _blockers, live_blockers = self.readiness(config)
        blockers = live_blockers if config.mode == "live" else _blockers
        if blockers:
            await self._audit(admin, "enable_refused", config, blockers=blockers)
            await self.repo.db.commit()  # the refusal itself is on record
            raise ProviderNotReadyError(blockers)
        config.status, config.updated_by_user_id = "enabled", admin.id
        await self.repo.save(config)
        await self._audit(admin, "enabled", config)
        return self.view(config)

    async def disable(self, admin: User, config_id: UUID) -> dict:
        """Blocks NEW payments only. The configuration (and its credentials) stay, so existing payments, refunds and webhooks
        keep working. If it was the store's preferred provider the store is left WITH NONE: nothing silently takes over."""
        config = await self._get(config_id, lock=True)
        was_preferred = config.is_preferred
        config.status, config.is_preferred, config.updated_by_user_id = "disabled", False, admin.id
        await self.repo.save(config)
        await self._audit(admin, "disabled", config, was_preferred=was_preferred)
        return self.view(config)

    async def prefer(self, admin: User, config_id: UUID) -> dict:
        config = await self._get(config_id, lock=True)
        spec = self.spec_of(config)
        if config.status != "enabled" or config.credentials_status != "valid":
            raise ProviderNotReadyError(["not_enabled" if config.status != "enabled" else "credentials_not_valid"])
        store_currency, _ = await StoreRepository(self.repo.db).effective_currency(get_settings().default_store_currency)
        if store_currency is None or store_currency not in set(config.currencies) & spec.currencies:
            raise ProviderConfigConflictError(
                "provider_currency_not_supported",
                f"{spec.label} is not enabled for this store's currency ({(store_currency or 'unset').upper()})")
        await self.repo.clear_preferred()
        config.is_preferred, config.updated_by_user_id = True, admin.id
        await self.repo.save(config)
        await self._audit(admin, "preferred", config)  # existing orders, payments and refunds are not touched
        return self.view(config)

    async def attest(self, admin: User, config_id: UUID, *, kind: str, reference: str) -> dict:
        config = await self._get(config_id, lock=True)
        if kind == "production_review" and config.sandbox_verified_at is None:
            raise ProviderConfigConflictError("sandbox_verification_required", "Record the sandbox verification before the production review")
        if kind == "sandbox_verified":
            config.sandbox_verified_at, config.sandbox_verification_ref = _now(), reference.strip()
        else:
            config.production_review_at, config.production_review_ref = _now(), reference.strip()
        config.updated_by_user_id = admin.id
        await self.repo.save(config)
        await self._audit(admin, "attested", config, kind=kind, reference=reference.strip()[:200])
        return self.view(config)

    def webhook_info(self, config: PaymentProviderConfig) -> dict:
        return self.view(config)["webhook"]

    # ------------------------------------------------------------ webhook verification feedback
    async def mark_webhook_verified(self, config_id: UUID) -> None:
        """Called when an event was received whose signature verified with this configuration's secret."""
        config = await self.repo.get(config_id, for_update=True)
        if config is not None and config.webhook_status != "verified":
            config.webhook_status, config.webhook_verified_at = "verified", _now()
            await self.repo.save(config)
            await self.audit.log(actor_user_id=None, action="provider_config.webhook_verified",
                                 target_type="payment_provider_config", target_id=config.id,
                                 details={"provider": config.provider, "mode": config.mode})
