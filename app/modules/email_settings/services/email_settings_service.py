"""Choosing and configuring the email service. Administrators pick one of a fixed set of providers (hosts and ports are presets in
app/core/email/providers.py), supply credentials, test them, and activate one; switching is a single call.

Safety rules, each covered by a test:
  * Credentials are encrypted at rest (app/core/secrets.py) and are write-only: no response, log line or audit entry carries one.
  * A provider can only become ACTIVE after a live connection test succeeds; editing the credentials of the ACTIVE provider is tested
    first and refused if it fails, so a typo cannot silently stop every customer email.
  * The test email goes to the administrator's own address only (no recipient can be supplied).
  * Failure reasons are fixed codes, never the mail server's own text."""

import json
import logging
from datetime import UTC, datetime

from app.core.config import HARDENED_ENVIRONMENTS, get_settings
from app.core.email.interface import (
    SMTP_ERROR_MESSAGES,
    SmtpConfig,
    SmtpEmailSender,
    smtp_error_code,
)
from app.core.email.providers import PROVIDERS, ProviderSpec
from app.core.redaction import mask_email
from app.core.secrets import get_cipher, hint
from app.modules.audit.services.audit_service import AuditService
from app.modules.auth.models.user import User
from app.modules.email_settings.exceptions.email_exceptions import (
    EmailProviderActiveError,
    EmailProviderNotFoundError,
    EmailProviderTestFailedError,
    InvalidEmailProviderSettingsError,
)
from app.modules.email_settings.models.email_provider_config import EmailProviderConfig
from app.modules.email_settings.repositories.email_provider_repository import (
    EmailProviderRepository,
)
from app.modules.email_settings.schemas.email_settings import (
    EffectiveEmail,
    EmailProviderSave,
    EmailSettingsRead,
    FieldSpec,
    ProbeResult,
    ProviderCatalogEntry,
    ProviderConfigRead,
)

logger = logging.getLogger(__name__)


def build_smtp_config(spec: ProviderSpec, row: EmailProviderConfig, username: str | None, password: str | None) -> SmtpConfig:
    return SmtpConfig(
        smtp_host=spec.host(row.mode), smtp_port=spec.port(row.mode), smtp_security=spec.security,
        smtp_username=username, smtp_password=password, email_from=row.from_email, email_from_name=row.from_name,
    )


def read_credentials(row: EmailProviderConfig) -> tuple[str, str]:
    data = json.loads(get_cipher().decrypt(row.credentials_ciphertext))
    return data["username"], data["password"]


class EmailSettingsService:
    def __init__(self, repository: EmailProviderRepository, audit: AuditService) -> None:
        self.repo = repository
        self.audit = audit

    # ------------------------------------------------------------------------------------------------ reading
    def catalog(self) -> list[ProviderCatalogEntry]:
        return [
            ProviderCatalogEntry(
                provider=spec.key, label=spec.label, mode_label=spec.mode_label, modes=spec.mode_options,
                fields=[
                    FieldSpec(name="username", label=spec.username_label),
                    FieldSpec(name="password", label=spec.password_label, secret=True),
                    FieldSpec(name="from_email", label="From address"),
                    FieldSpec(name="from_name", label="From name"),
                ],
                host_by_mode={(m or "default"): spec.host(m) for m in spec.modes()},
                notes=list(spec.notes),
            )
            for spec in PROVIDERS.values()
        ]

    def _read(self, spec: ProviderSpec, row: EmailProviderConfig | None) -> ProviderConfigRead:
        if row is None:
            return ProviderConfigRead(provider=spec.key, label=spec.label, configured=False)
        warnings: list[str] = []
        if not spec.delivers(row.mode):
            warnings.append("Sandbox service: messages are captured in a test inbox and are NOT delivered to customers.")
        _, _, domain = row.username_hint.partition("@")
        if domain and spec.key in ("google", "zoho") and row.from_email.split("@")[-1].lower() != domain.lower():
            warnings.append("The From address is on a different domain than the account; the provider may rewrite or reject it.")
        return ProviderConfigRead(
            id=row.id, provider=spec.key, label=spec.label, configured=True, mode=row.mode, username_hint=row.username_hint,
            password_set=True, password_hint=row.password_hint, from_email=row.from_email, from_name=row.from_name,
            is_active=row.is_active, delivers_to_real_recipients=spec.delivers(row.mode), last_test_at=row.last_test_at,
            last_test_ok=row.last_test_ok, last_test_error=row.last_test_error, last_sent_at=row.last_sent_at,
            last_error_at=row.last_error_at, last_error_code=row.last_error_code, warnings=warnings,
        )

    async def read_settings(self) -> EmailSettingsRead:
        rows = {r.provider: r for r in await self.repo.list_all()}
        providers = [self._read(spec, rows.get(spec.key)) for spec in PROVIDERS.values()]
        active = next((r for r in rows.values() if r.is_active), None)
        if active is not None:
            spec = PROVIDERS[active.provider]
            effective = EffectiveEmail(source="database", provider=spec.key, label=spec.label,
                                       delivers_to_real_recipients=spec.delivers(active.mode))
        else:
            settings = get_settings()
            smtp = settings.email_backend == "smtp"
            effective = EffectiveEmail(
                source="environment", provider=None, delivers_to_real_recipients=smtp,
                label="SMTP from the server's environment settings" if smtp else "Log only (no email is delivered)",
            )
        return EmailSettingsRead(effective=effective, providers=providers)

    async def read_one(self, provider: str) -> ProviderConfigRead:
        return self._read(PROVIDERS[provider], await self.repo.get(provider))

    # ------------------------------------------------------------------------------------------------ saving
    @staticmethod
    def _validated_mode(spec: ProviderSpec, mode: str | None) -> str | None:
        if spec.mode_options:
            if mode not in spec.mode_options:
                raise InvalidEmailProviderSettingsError(f"{spec.mode_label} must be one of: {', '.join(spec.mode_options)}")
            return mode
        if mode is not None:
            raise InvalidEmailProviderSettingsError(f"{spec.label} has no {mode!r} option")
        return None

    async def _probe(self, config: SmtpConfig) -> str | None:
        """None if the account works, otherwise a fixed error code. Nothing is sent."""
        try:
            await SmtpEmailSender(config).probe()
            return None
        except Exception as exc:  # noqa: BLE001 - any failure is a failed test, reported by code only
            code = smtp_error_code(exc)
            logger.info("email provider connection test failed: %s (%s)", code, type(exc).__name__)
            return code

    @staticmethod
    def _failure(code: str) -> EmailProviderTestFailedError:
        return EmailProviderTestFailedError(code, SMTP_ERROR_MESSAGES.get(code, SMTP_ERROR_MESSAGES["unknown"]))

    async def save(self, provider: str, payload: EmailProviderSave, admin: User) -> ProviderConfigRead:
        spec = PROVIDERS[provider]
        mode = self._validated_mode(spec, payload.mode)
        row = await self.repo.get(provider)

        username = payload.username or None
        password = payload.password.get_secret_value() if payload.password else None
        if row is not None and (username is None or password is None):
            try:
                old_user, old_password = read_credentials(row)
            except Exception:  # noqa: BLE001 - stored credentials unreadable: the caller must supply both again
                old_user = old_password = None
            username = username or old_user
            password = password or old_password
        if username is None and spec.username_default:
            username = spec.username_default.get(mode) or None
        if not username or not password:
            raise InvalidEmailProviderSettingsError(
                f"{spec.username_label} and {spec.password_label.lower()} are required the first time (and whenever the stored ones cannot be read)"
            )

        cipher = get_cipher()  # 503 if PROVIDER_SECRETS_KEY is not set: credentials are never stored in the clear
        credentials_changed = row is None or mode != row.mode or payload.username is not None or payload.password is not None
        candidate = EmailProviderConfig(
            provider=provider, mode=mode, from_email=str(payload.from_email), from_name=payload.from_name,
        )
        if row is not None and row.is_active and credentials_changed:
            code = await self._probe(build_smtp_config(spec, candidate, username, password))
            if code is not None:  # the working configuration stays exactly as it was
                raise self._failure(code)

        ciphertext = cipher.encrypt(json.dumps({"username": username, "password": password}))
        values = dict(
            mode=mode, credentials_ciphertext=ciphertext, username_hint=mask_email(username) if "@" in username else f"{username[:1]}***",
            password_hint=hint(password), from_email=str(payload.from_email), from_name=payload.from_name, updated_by_user_id=admin.id,
        )
        if row is None:
            row = await self.repo.add(EmailProviderConfig(provider=provider, **values))
        else:
            for key, value in values.items():
                setattr(row, key, value)
        if credentials_changed:
            row.last_test_at = row.last_test_ok = row.last_test_error = None
        await self.repo.flush()
        await self.audit.log(
            actor_user_id=admin.id, action="email_provider.saved", target_type="email_provider", target_id=provider,
            details={"provider": provider, "mode": mode, "credentials_changed": credentials_changed},
        )
        return self._read(spec, row)

    # ------------------------------------------------------------------------------------------------ testing
    async def _row(self, provider: str) -> EmailProviderConfig:
        row = await self.repo.get(provider)
        if row is None:
            raise EmailProviderNotFoundError(PROVIDERS[provider].label)
        return row

    def _record_test(self, row: EmailProviderConfig, code: str | None) -> None:
        row.last_test_at = datetime.now(UTC)
        row.last_test_ok = code is None
        row.last_test_error = code

    async def test(self, provider: str, admin: User) -> ProbeResult:
        spec, row = PROVIDERS[provider], await self._row(provider)
        username, password = read_credentials(row)
        code = await self._probe(build_smtp_config(spec, row, username, password))
        self._record_test(row, code)
        await self.repo.flush()
        await self.audit.log(
            actor_user_id=admin.id, action="email_provider.tested", target_type="email_provider", target_id=provider,
            details={"ok": code is None, "error_code": code},
        )
        return ProbeResult(ok=code is None, error_code=code,
                           message="Connected and signed in successfully." if code is None else SMTP_ERROR_MESSAGES[code])

    async def send_test_email(self, provider: str, admin: User) -> ProbeResult:
        """Sends one message to THE ADMINISTRATOR'S OWN address through this provider (active or not)."""
        spec, row = PROVIDERS[provider], await self._row(provider)
        username, password = read_credentials(row)
        sender = SmtpEmailSender(build_smtp_config(spec, row, username, password))
        store = get_settings().store_name
        code = None
        try:
            await sender.send(
                to=admin.email, subject=f"Test email from {store}",
                body=f"This is a test message from {store}, sent through {spec.label}.\n\nIf you can read it, this email service works.\n",
            )
        except Exception as exc:  # noqa: BLE001
            code = smtp_error_code(exc)
            logger.info("email provider test message failed: %s (%s)", code, type(exc).__name__)
        self._record_test(row, code)
        await self.repo.flush()
        await self.audit.log(
            actor_user_id=admin.id, action="email_provider.test_email", target_type="email_provider", target_id=provider,
            details={"ok": code is None, "error_code": code},
        )
        message = f"A test message was sent to {mask_email(admin.email)}." if code is None else SMTP_ERROR_MESSAGES[code]
        if code is None and not spec.delivers(row.mode):
            message += " (This is a sandbox service: look for it in your Mailtrap inbox, not your mailbox.)"
        return ProbeResult(ok=code is None, error_code=code, message=message)

    # ------------------------------------------------------------------------------------------------ switching
    async def activate(self, provider: str, admin: User) -> ProviderConfigRead:
        spec, row = PROVIDERS[provider], await self._row(provider)
        username, password = read_credentials(row)
        code = await self._probe(build_smtp_config(spec, row, username, password))  # a live check, never a stale result
        self._record_test(row, code)
        if code is not None:
            await self.repo.flush()
            raise self._failure(code)
        previous = await self.repo.get_active()
        await self.repo.deactivate_all()
        row.is_active = True
        await self.repo.flush()
        await self.audit.log(
            actor_user_id=admin.id, action="email_provider.activated", target_type="email_provider", target_id=provider,
            details={"provider": provider, "previous": previous.provider if previous else None},
        )
        result = self._read(spec, row)
        if not spec.delivers(row.mode) and get_settings().environment in HARDENED_ENVIRONMENTS:
            result.warnings.append("This is a sandbox service in a production-like environment: customers will receive nothing.")
        return result

    async def deactivate(self, admin: User) -> EmailSettingsRead:
        previous = await self.repo.get_active()
        await self.repo.deactivate_all()
        if previous is not None:
            await self.audit.log(actor_user_id=admin.id, action="email_provider.deactivated", target_type="email_provider",
                                 target_id=previous.provider, details={"provider": previous.provider})
        return await self.read_settings()

    async def delete(self, provider: str, admin: User) -> None:
        row = await self._row(provider)
        if row.is_active:
            raise EmailProviderActiveError()
        await self.repo.delete(row)
        await self.audit.log(actor_user_id=admin.id, action="email_provider.deleted", target_type="email_provider",
                             target_id=provider, details={"provider": provider})
