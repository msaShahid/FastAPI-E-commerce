import logging
from abc import ABC, abstractmethod
from functools import lru_cache

from app.core.config import get_settings

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
        logger.info("EMAIL (logged, not sent) to=%s subject=%r\n%s", to, subject, body)


@lru_cache
def get_email_sender() -> EmailSender:
    if get_settings().environment == "production":
        logger.warning(
            "No real email provider is configured: emails are only written to the log. "
            "Implement EmailSender and return it from get_email_sender()."
        )
    return LoggingEmailSender()
