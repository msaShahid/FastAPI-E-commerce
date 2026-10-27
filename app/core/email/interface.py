import asyncio
import logging
import smtplib
import ssl
from abc import ABC, abstractmethod
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr
from functools import lru_cache

from app.core.config import HARDENED_ENVIRONMENTS, get_settings
from app.core.redaction import mask_email

logger = logging.getLogger(__name__)


class EmailSender(ABC):
    @abstractmethod
    async def send(self, *, to: str, subject: str, body: str) -> None:
        """Send one plain-text email. Raise on failure so the job is retried."""


class LoggingEmailSender(EmailSender):
    """
    Writes the email to the log instead of sending it. This is the default so
    the whole flow works with no mail provider; to send real mail, implement
    EmailSender (SMTP, SES, SendGrid ...) and return it from get_email_sender().
    """

    async def send(self, *, to: str, subject: str, body: str) -> None:
        # The recipient is masked. The body (which can carry a personal link) is logged only where EMAIL_LOG_BODIES allows it
        # (default: development and testing, so a developer can click a reset link; never in staging/production).
        logger.info("EMAIL (logged, not sent) to=%s subject=%r", mask_email(to), subject)
        if get_settings().log_email_bodies():
            logger.info("EMAIL body:\n%s", body)


@dataclass(frozen=True)
class SmtpConfig:
    """Connection settings for one SMTP account. The attribute names match Settings, so either can drive SmtpEmailSender."""

    smtp_host: str
    smtp_port: int
    smtp_security: str
    smtp_username: str | None
    smtp_password: str | None
    email_from: str
    email_from_name: str
    smtp_timeout_seconds: int = 15

    def __repr__(self) -> str:  # the password must never reach a log through a stray repr
        return f"SmtpConfig(host={self.smtp_host!r}, port={self.smtp_port}, security={self.smtp_security!r})"


SMTP_ERROR_MESSAGES = {
    "auth_failed": "The provider rejected the username or password.",
    "connection_failed": "Could not connect to the provider's mail server (network, firewall or timeout).",
    "tls_failed": "The secure (TLS) connection to the provider could not be established.",
    "tls_unsupported": "The provider's server does not offer the expected secure connection.",
    "sender_refused": "The provider refused the From address (it must belong to the account or be a verified alias).",
    "recipient_refused": "The provider refused the recipient address.",
    "invalid_message": "The message could not be built (an invalid address or header).",
    "smtp_error": "The provider's mail server reported an error.",
    "unknown": "Sending failed for an unknown reason.",
}


def smtp_error_code(exc: BaseException) -> str:
    """A SAFE, fixed code for a failure. The server's own wording is never passed on (it can echo addresses or account details)."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "auth_failed"
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "sender_refused"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "recipient_refused"
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return "tls_unsupported"
    if isinstance(exc, ssl.SSLError):
        return "tls_failed"
    if isinstance(exc, ValueError):
        return "invalid_message"
    if isinstance(exc, smtplib.SMTPConnectError | smtplib.SMTPServerDisconnected):
        return "connection_failed"
    if isinstance(exc, smtplib.SMTPException):  # (an OSError subclass: it must be tested BEFORE the network errors below)
        return "smtp_error"
    if isinstance(exc, TimeoutError | OSError):
        return "connection_failed"
    return "unknown"


class SmtpEmailSender(EmailSender):
    """Sends plain-text mail through any SMTP server (Amazon SES, Zoho, Brevo, SendGrid, a company relay...).

    Uses only the standard library (smtplib in a worker thread, with a timeout), so there is no extra dependency. Any failure
    raises, and the background job that called it is retried with backoff. Header values come through EmailMessage, which
    refuses embedded newlines (no header injection)."""

    def __init__(self, settings) -> None:
        self.settings = settings

    def _build(self, to: str, subject: str, body: str) -> EmailMessage:
        s = self.settings
        message = EmailMessage()
        message["From"] = formataddr((s.email_from_name, s.email_from))
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        return message

    def _session(self, message: EmailMessage | None) -> None:
        s = self.settings
        context = ssl.create_default_context()
        if s.smtp_security == "ssl":
            server = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout_seconds, context=context)
        else:
            server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=s.smtp_timeout_seconds)
        with server:
            if s.smtp_security == "starttls":
                server.starttls(context=context)
            if s.smtp_username:
                server.login(s.smtp_username, s.smtp_password)
            if message is not None:
                server.send_message(message)

    def _send_blocking(self, message: EmailMessage) -> None:
        self._session(message)

    async def probe(self) -> None:
        """Connect, secure the connection and sign in, WITHOUT sending anything. Raises on any problem."""
        await asyncio.to_thread(self._session, None)

    async def send(self, *, to: str, subject: str, body: str) -> None:
        message = self._build(to, subject, body)  # raises ValueError on a malformed header, before anything is sent
        await asyncio.to_thread(self._send_blocking, message)
        logger.info("EMAIL sent to=%s subject=%r", mask_email(to), subject)


@lru_cache
def get_email_sender() -> EmailSender:
    settings = get_settings()
    if settings.email_backend == "smtp":
        return SmtpEmailSender(settings)
    if settings.environment in HARDENED_ENVIRONMENTS:
        logger.warning(
            "EMAIL_BACKEND=log: emails (including password-reset links) are NOT delivered to anyone in this environment. "
            "Set EMAIL_BACKEND=smtp with SMTP_HOST and EMAIL_FROM."
        )
    return LoggingEmailSender()
