"""
Encryption of provider credentials at rest.

SECURITY MODEL (and its limits, stated plainly):
  * Credentials are encrypted with Fernet (AES-128-CBC + HMAC-SHA256, from the `cryptography` package) before they touch
    the database. Only ciphertext and a non-secret hint are stored. A database dump or backup alone does NOT reveal them.
  * The master key lives OUTSIDE the database, in the environment (PROVIDER_SECRETS_KEY). Anyone who can read BOTH the
    database and the application's environment can decrypt. This is NOT a hardware/KMS boundary: for production prefer
    injecting the key from a secrets manager/KMS at deploy time (see docs/PAYMENT_PROVIDERS.md). If the key is not
    configured, no credential can be stored (503), instead of falling back to plaintext.
  * Rotation: set the new key as PROVIDER_SECRETS_KEY (with a new PROVIDER_SECRETS_KEY_ID) and keep the old one in
    PROVIDER_SECRETS_PREVIOUS_KEYS ("id:key,id:key") so existing records still decrypt; re-encrypt records, then drop the old key.
  * Secrets are never logged, returned by any API, or included in error messages (errors carry only a code).
"""

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings
from app.core.exceptions import ServiceUnavailableError


class SecretStorageNotConfiguredError(ServiceUnavailableError):
    code = "secret_storage_not_configured"

    def __init__(self) -> None:
        super().__init__("Credential storage is not configured on this deployment (PROVIDER_SECRETS_KEY is not set)")


class SecretUnreadableError(ServiceUnavailableError):
    """A stored secret cannot be decrypted (key missing or wrong). Reported as a degraded provider, never with detail."""

    code = "secret_unreadable"

    def __init__(self) -> None:
        super().__init__("A stored credential cannot be decrypted with the configured keys")


def parse_previous_keys(raw: str) -> dict[str, str]:
    pairs = [p.strip() for p in raw.split(",") if p.strip()]
    return dict(p.split(":", 1) for p in pairs if ":" in p)


class SecretCipher:
    def __init__(self, current_id: str, current_key: str, previous: dict[str, str] | None = None) -> None:
        self.current_id = current_id
        self._fernets = {current_id: Fernet(current_key.encode())}
        for key_id, key in (previous or {}).items():
            self._fernets[key_id] = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> str:
        return f"{self.current_id}:{self._fernets[self.current_id].encrypt(plaintext.encode()).decode()}"

    def decrypt(self, blob: str) -> str:
        key_id, _, token = blob.partition(":")
        fernet = self._fernets.get(key_id)
        if fernet is None:
            raise SecretUnreadableError()
        try:
            return fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise SecretUnreadableError() from exc

    def key_id_of(self, blob: str) -> str:
        return blob.partition(":")[0]


def get_cipher() -> SecretCipher:
    settings = get_settings()
    if settings.provider_secrets_key is None:
        raise SecretStorageNotConfiguredError()
    return SecretCipher(
        settings.provider_secrets_key_id, settings.provider_secrets_key.get_secret_value(),
        parse_previous_keys(settings.provider_secrets_previous_keys),
    )


def hint(secret: str) -> str:
    """A non-secret hint for the UI: only the last 4 characters (never enough to use the secret)."""
    return f"...{secret[-4:]}" if len(secret) >= 8 else "...set"
