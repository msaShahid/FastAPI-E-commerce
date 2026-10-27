"""The text of the account emails. Plain text on purpose: it cannot be hijacked by markup, renders everywhere and is easy to audit.

Every link points at the STOREFRONT (FRONTEND_BASE_URL); the storefront page reads the token from the query string and calls the
matching API endpoint. Wording never reveals whether an address has an account."""

from urllib.parse import quote

from app.core.config import get_settings
from app.core.redaction import mask_email


def _link(path: str, token: str) -> str:
    return f"{get_settings().frontend_base_url}{path}?token={quote(token, safe='')}"


def _wrap(body: str) -> str:
    return f"{body.strip()}\n\n-- {get_settings().store_name}\n"


def password_reset(token: str) -> tuple[str, str]:
    minutes = get_settings().password_reset_token_minutes
    return (
        f"Reset your {get_settings().store_name} password",
        _wrap(
            f"We received a request to reset the password for your account.\n\nChoose a new password here (the link works once and "
            f"expires in {minutes} minutes):\n{_link('/reset-password', token)}\n\nIf you did not ask for this, ignore this email: "
            "your password has not changed."
        ),
    )


def email_verification(token: str) -> tuple[str, str]:
    hours = get_settings().email_verification_token_hours
    return (
        f"Confirm your email address for {get_settings().store_name}",
        _wrap(
            f"Please confirm this is your email address (the link works once and expires in {hours} hours):\n"
            f"{_link('/verify-email', token)}\n\nIf you did not create an account, ignore this email."
        ),
    )


def email_change_confirmation(token: str) -> tuple[str, str]:
    minutes = get_settings().email_change_token_minutes
    return (
        f"Confirm your new email address for {get_settings().store_name}",
        _wrap(
            f"Someone asked to use this address for a {get_settings().store_name} account. To confirm (the link works once and "
            f"expires in {minutes} minutes):\n{_link('/confirm-email-change', token)}\n\nIf this was not you, ignore this email."
        ),
    )


def email_change_requested(new_email: str) -> tuple[str, str]:
    return (
        f"Email change requested on your {get_settings().store_name} account",
        _wrap(
            f"A change of your account's email address to {mask_email(new_email)} was requested. Nothing changes until that address "
            "is confirmed.\n\nIf this was not you, change your password now."
        ),
    )


def email_changed(new_email: str) -> tuple[str, str]:
    return (
        f"Your {get_settings().store_name} email address was changed",
        _wrap(
            f"The email address on your account is now {mask_email(new_email)}. You will no longer receive messages here.\n\n"
            "If this was not you, contact the store immediately."
        ),
    )


def password_changed() -> tuple[str, str]:
    return (
        f"Your {get_settings().store_name} password was changed",
        _wrap(
            "The password on your account was just changed and you were signed out everywhere.\n\nIf this was not you, reset your "
            "password immediately using 'Forgot password' and contact the store."
        ),
    )
