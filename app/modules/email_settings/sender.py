"""Which email sender the background jobs use RIGHT NOW: the administrator-selected provider if one is active, otherwise the
deployment's environment configuration (`fallback`). Resolved on every send, so switching providers takes effect immediately."""

import logging
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.email.interface import EmailSender, SmtpEmailSender, smtp_error_code
from app.core.email.providers import PROVIDERS
from app.modules.email_settings.repositories.email_provider_repository import (
    EmailProviderRepository,
)
from app.modules.email_settings.services.email_settings_service import (
    build_smtp_config,
    read_credentials,
)

logger = logging.getLogger(__name__)


class RecordingSender(EmailSender):
    """Wraps the real sender and notes the outcome on the configuration (last success / last failure CODE) so an administrator can
    see a broken email service without reading logs. Recording never changes the outcome of the send itself."""

    def __init__(self, inner: EmailSender, config_id, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.inner, self.config_id, self.session_factory = inner, config_id, session_factory

    async def _record(self, code: str | None) -> None:
        try:
            async with self.session_factory() as session, session.begin():
                await EmailProviderRepository(session).record_send(self.config_id, code)
        except Exception:  # noqa: BLE001
            logger.warning("could not record the email send outcome", exc_info=True)

    async def send(self, *, to: str, subject: str, body: str) -> None:
        try:
            await self.inner.send(to=to, subject=subject, body=body)
        except Exception as exc:
            await self._record(smtp_error_code(exc))
            raise
        await self._record(None)


async def resolve_email_sender(
    session_factory: async_sessionmaker[AsyncSession], fallback: Callable[[], EmailSender]
) -> EmailSender:
    async with session_factory() as session:
        row = await EmailProviderRepository(session).get_active()
        if row is None:
            return fallback()
        # A stored credential that cannot be decrypted raises here: the job fails visibly and retries, instead of silently
        # sending through some other account.
        username, password = read_credentials(row)
        config = build_smtp_config(PROVIDERS[row.provider], row, username, password)
        config_id = row.id
    return RecordingSender(SmtpEmailSender(config), config_id, session_factory)
