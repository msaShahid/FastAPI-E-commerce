import logging
from abc import ABC, abstractmethod
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
        # The recipient is masked and the body is only visible at DEBUG: an email can carry personal data or a link.
        logger.info("EMAIL (logged, not sent) to=%s subject=%r", mask_email(to), subject)
        logger.debug("EMAIL body:\n%s", body)


@lru_cache
def get_email_sender() -> EmailSender:
    if get_settings().environment in HARDENED_ENVIRONMENTS:
        logger.warning(
            "No real email provider is configured: emails are only written to the log. "
            "Implement EmailSender and return it from get_email_sender()."
        )
    return LoggingEmailSender()
