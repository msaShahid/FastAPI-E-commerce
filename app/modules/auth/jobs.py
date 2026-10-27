"""Background jobs that SEND the account emails. Tokens are created here, at send time, so the secret exists only in the email and
in memory: it is never in the job queue's payload. A failure to send rolls the token back and the job is retried."""

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.email.interface import get_email_sender
from app.core.jobs.registry import job_handler
from app.core.redaction import mask_email
from app.modules.auth import emails
from app.modules.auth.repositories.auth_repository import AuthRepository
from app.modules.auth.services.account_service import (
    EMAIL_CHANGE,
    EMAIL_VERIFICATION,
    PASSWORD_RESET,
    SEND_ACCOUNT_NOTICE,
    SEND_EMAIL_CHANGE,
    SEND_EMAIL_VERIFICATION,
    SEND_PASSWORD_RESET,
    hash_token,
    new_token,
    token_expiry,
)
from app.modules.email_settings.sender import resolve_email_sender

logger = logging.getLogger(__name__)


async def _sender(session_factory):
    """The administrator-selected email service if one is active, else the environment one (looked up on every send)."""
    return await resolve_email_sender(session_factory, get_email_sender)


async def _issue(repo: AuthRepository, *, user_id: UUID, purpose: str, email: str) -> str:
    token = new_token()
    await repo.create_auth_token(user_id=user_id, purpose=purpose, token_hash=hash_token(token), email=email, expires_at=token_expiry(purpose))
    return token


@job_handler(SEND_PASSWORD_RESET)
async def send_password_reset(payload: dict, session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session, session.begin():
        repo = AuthRepository(session)
        user = await repo.find_user_by_email_ci(payload["email"])
        if user is None or not user.is_active:
            logger.info("password reset requested for an address with no active account (%s): nothing sent", mask_email(payload["email"]))
            return
        since = datetime.now(UTC) - timedelta(hours=1)
        if await repo.count_recent_auth_tokens(user_id=user.id, purpose=PASSWORD_RESET, since=since) >= get_settings().password_reset_emails_per_hour:
            logger.warning("password reset email limit reached for user %s: nothing sent", user.id)
            return
        token = await _issue(repo, user_id=user.id, purpose=PASSWORD_RESET, email=user.email)
        subject, body = emails.password_reset(token)
        await (await _sender(session_factory)).send(to=user.email, subject=subject, body=body)


@job_handler(SEND_EMAIL_VERIFICATION)
async def send_email_verification(payload: dict, session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session, session.begin():
        repo = AuthRepository(session)
        user = await repo.get_user_by_id(UUID(payload["user_id"]))
        if user is None or not user.is_active or user.email_verified_at is not None:
            return
        token = await _issue(repo, user_id=user.id, purpose=EMAIL_VERIFICATION, email=user.email)
        subject, body = emails.email_verification(token)
        await (await _sender(session_factory)).send(to=user.email, subject=subject, body=body)


@job_handler(SEND_EMAIL_CHANGE)
async def send_email_change(payload: dict, session_factory: async_sessionmaker[AsyncSession]) -> None:
    new_email = payload["new_email"]
    async with session_factory() as session, session.begin():
        repo = AuthRepository(session)
        user = await repo.get_user_by_id(UUID(payload["user_id"]))
        if user is None or not user.is_active:
            return
        taken = await repo.find_user_by_email_ci(new_email)
        if taken is not None and taken.id != user.id:
            logger.info("email change to an address already in use (%s): nothing sent", mask_email(new_email))  # no enumeration
            return
        token = await _issue(repo, user_id=user.id, purpose=EMAIL_CHANGE, email=new_email)
        sender = await _sender(session_factory)
        subject, body = emails.email_change_confirmation(token)
        await sender.send(to=new_email, subject=subject, body=body)
        subject, body = emails.email_change_requested(new_email)
        await sender.send(to=user.email, subject=subject, body=body)


@job_handler(SEND_ACCOUNT_NOTICE)
async def send_account_notice(payload: dict, session_factory: async_sessionmaker[AsyncSession]) -> None:
    kind = payload["kind"]
    if kind == "email_changed":  # sent to the OLD address, which is no longer on the account
        subject, body = emails.email_changed(payload["new_email"])
        await (await _sender(session_factory)).send(to=payload["to"], subject=subject, body=body)
        return
    async with session_factory() as session:
        user = await AuthRepository(session).get_user_by_id(UUID(payload["user_id"]))
    if user is None or not user.is_active:
        return
    if kind == "password_changed":
        subject, body = emails.password_changed()
        await (await _sender(session_factory)).send(to=user.email, subject=subject, body=body)
