"""
Which provider handles what. Three questions, answered separately and never mixed:

  * NEW payment  -> select_for_new_payment(): the store's PREFERRED, ENABLED, validated configuration, for a currency it supports,
                    in a mode that is allowed. If anything is wrong the payment is REFUSED with a clear code. There is NEVER a
                    silent switch to another provider (no failover without a separately approved design).
  * EXISTING payment (refund, cancel, retrieve) -> provider_for_payment(): the configuration that CREATED the payment, even if
                    it is disabled now or another provider is preferred. Disabling never breaks reconciliation.
  * WEBHOOK      -> webhook_verifiers(): every stored configuration of the provider named in the URL (disabled ones too; a
                    rotated-away secret during its overlap). The URL only names a provider: an event is accepted ONLY if its
                    signature verifies with one of that provider's own stored webhook secrets.
When NO configuration exists at all, the environment-configured Stripe is used exactly as before (backwards compatible).
"""

import json
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.payments.interface import PaymentProvider
from app.core.payments.registry import get_spec
from app.core.secrets import SecretCipher, get_cipher
from app.modules.payments.exceptions.provider_exceptions import PaymentProviderUnavailableError
from app.modules.payments.models.provider_config import PaymentProviderConfig
from app.modules.payments.repositories.provider_config_repository import ProviderConfigRepository


@dataclass
class SelectedProvider:
    provider: PaymentProvider
    config: PaymentProviderConfig


def build_provider(config: PaymentProviderConfig, cipher: SecretCipher, *, previous_webhook_secret: bool = False) -> PaymentProvider:
    """An adapter for a stored configuration, whatever its state (reconciliation must keep working when it is disabled)."""
    spec = get_spec(config.provider)
    credentials = json.loads(cipher.decrypt(config.credentials_ciphertext))
    blob = config.previous_webhook_secret_ciphertext if previous_webhook_secret else config.webhook_secret_ciphertext
    return spec.factory(credentials, cipher.decrypt(blob) if blob else None)


class ProviderResolver:
    def __init__(self, db: AsyncSession, *, cipher_factory=get_cipher) -> None:
        self.repo = ProviderConfigRepository(db)
        self._cipher_factory = cipher_factory

    async def uses_configurations(self) -> bool:
        return await self.repo.any_exists()

    async def select_for_new_payment(self, currency: str) -> SelectedProvider:
        config = await self.repo.get_preferred()
        if config is None:
            raise PaymentProviderUnavailableError(
                "no_payment_provider_selected", "No payment provider is selected for this store. An administrator must enable one and choose it.")
        spec = get_spec(config.provider)
        if config.status != "enabled":
            raise PaymentProviderUnavailableError("provider_disabled", f"{spec.label} is not enabled, so no new payments can be started with it")
        if config.credentials_status != "valid":
            raise PaymentProviderUnavailableError(
                "provider_credentials_not_valid", f"The {spec.label} credentials are not validated: an administrator must validate them again")
        if currency.lower() not in set(config.currencies) & spec.currencies:
            raise PaymentProviderUnavailableError(
                "provider_currency_not_supported", f"{spec.label} is not enabled for {currency.upper()} on this store")
        if config.mode == "live" and not get_settings().live_payments_enabled:
            raise PaymentProviderUnavailableError("provider_mode_not_allowed", "Live payments are not enabled on this deployment")
        return SelectedProvider(build_provider(config, self._cipher_factory()), config)

    async def provider_for_config(self, config_id) -> PaymentProvider | None:
        config = await self.repo.get(config_id)
        return None if config is None else build_provider(config, self._cipher_factory())

    async def webhook_verifiers(self, provider_name: str) -> list[tuple[PaymentProviderConfig, PaymentProvider]]:
        cipher = self._cipher_factory()
        verifiers = []
        for config in await self.repo.list_for_provider(provider_name):
            if config.webhook_secret_ciphertext:
                verifiers.append((config, build_provider(config, cipher)))
            if config.previous_webhook_secret_ciphertext:  # rotation overlap: the old secret still verifies until retired
                verifiers.append((config, build_provider(config, cipher, previous_webhook_secret=True)))
        return verifiers
