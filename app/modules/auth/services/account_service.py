"""Account recovery and sensitive account changes: password reset, password change, email verification, email change.

Security properties, all covered by tests:
  * Secrets are 256 random bits; only their SHA-256 is stored; each works ONCE, for ONE purpose, until it expires, and only while
    the address it was issued for is still the account's address.
  * 'Forgot password' answers the same whether or not the address has an account, and does the same work either way.
  * A password reset or change signs the account out everywhere and notifies the owner.
  * An email change needs the current password, is confirmed from the NEW address, and notifies the OLD one.
"""

import hashlib
import secrets
import time
from datetime import UTC, datetime, timedelta

from app.core.jobs.queue import JobQueue
from app.core.security import hash_password, verify_password
from app.modules.audit.services.audit_service import AuditService
from app.modules.auth.exceptions.auth_exceptions import (
    CurrentPasswordIncorrectError,
    EmailAlreadyExistsError,
    InvalidAccountTokenError,
    SamePasswordError,
)
from app.modules.auth.models.user import User
from app.modules.auth.repositories.auth_repository import AuthRepository
from app.modules.auth.schemas.auth import TokenPair
from app.modules.auth.services.auth_service import AuthService

PASSWORD_RESET = "password_reset"
EMAIL_VERIFICATION = "email_verification"
EMAIL_CHANGE = "email_change"

SEND_PASSWORD_RESET = "send_password_reset"
SEND_EMAIL_VERIFICATION = "send_email_verification"
SEND_EMAIL_CHANGE = "send_email_change"
SEND_ACCOUNT_NOTICE = "send_account_notice"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def _minute_bucket() -> int:
    return int(time.time() // 60)


class AccountService:
    def __init__(self, repository: AuthRepository, auth: AuthService, audit: AuditService, jobs: JobQueue) -> None:
        self.repository = repository
        self.auth = auth
        self.audit = audit
        self.jobs = jobs

    # ------------------------------------------------------------------------------------------------ password reset
    async def request_password_reset(self, email: str) -> None:
        """Always the same work and the same answer. Whether an account exists is decided later, in the job, never here."""
        key = hashlib.sha256(email.lower().encode()).hexdigest()
        await self.jobs.enqueue(SEND_PASSWORD_RESET, {"email": email}, dedupe_key=f"pwreset:{key}:{_minute_bucket()}")

    async def reset_password(self, *, token: str, new_password: str) -> None:
        row = await self.repository.consume_auth_token(token_hash=hash_token(token), purpose=PASSWORD_RESET)
        user = await self.repository.get_user_by_id(row.user_id) if row else None
        if row is None or user is None or not user.is_active or user.email != row.email:
            raise InvalidAccountTokenError()
        user.password_hash = hash_password(new_password)
        if user.email_verified_at is None:  # clicking a link sent to the address proves control of it
            user.email_verified_at = datetime.now(UTC)
        await self.repository.invalidate_auth_tokens(user_id=user.id, purposes=(PASSWORD_RESET,))
        await self.repository.revoke_all_refresh_tokens(user.id)
        await self.audit.log(actor_user_id=user.id, action="auth.password_reset", target_type="user", target_id=user.id)
        await self.jobs.enqueue(SEND_ACCOUNT_NOTICE, {"user_id": str(user.id), "kind": "password_changed"})

    # ------------------------------------------------------------------------------------------------ password change
    async def change_password(self, *, user: User, current_password: str, new_password: str) -> TokenPair:
        if not verify_password(current_password, user.password_hash):
            raise CurrentPasswordIncorrectError()
        if current_password == new_password:
            raise SamePasswordError()
        user.password_hash = hash_password(new_password)
        await self.repository.invalidate_auth_tokens(user_id=user.id, purposes=(PASSWORD_RESET,))
        await self.repository.revoke_all_refresh_tokens(user.id)  # every other device is signed out
        tokens = await self.auth.issue_tokens(user)  # this device continues with a fresh pair
        await self.audit.log(actor_user_id=user.id, action="auth.password_changed", target_type="user", target_id=user.id)
        await self.jobs.enqueue(SEND_ACCOUNT_NOTICE, {"user_id": str(user.id), "kind": "password_changed"})
        return tokens

    # ------------------------------------------------------------------------------------------------ email verification
    async def request_email_verification(self, user: User) -> None:
        if user.email_verified_at is not None:
            return
        await self.jobs.enqueue(SEND_EMAIL_VERIFICATION, {"user_id": str(user.id)}, dedupe_key=f"verify:{user.id}:{_minute_bucket()}")

    async def verify_email(self, *, token: str) -> None:
        row = await self.repository.consume_auth_token(token_hash=hash_token(token), purpose=EMAIL_VERIFICATION)
        user = await self.repository.get_user_by_id(row.user_id) if row else None
        if row is None or user is None or not user.is_active or user.email != row.email:
            raise InvalidAccountTokenError()
        if user.email_verified_at is None:
            user.email_verified_at = datetime.now(UTC)
            await self.audit.log(actor_user_id=user.id, action="auth.email_verified", target_type="user", target_id=user.id)

    # ------------------------------------------------------------------------------------------------ email change
    async def request_email_change(self, *, user: User, new_email: str, password: str) -> None:
        if not verify_password(password, user.password_hash):
            raise CurrentPasswordIncorrectError()
        if new_email.lower() == user.email.lower():
            return  # nothing to change; same answer as any other request
        key = hashlib.sha256(f"{user.id}:{new_email.lower()}".encode()).hexdigest()
        await self.jobs.enqueue(
            SEND_EMAIL_CHANGE, {"user_id": str(user.id), "new_email": new_email}, dedupe_key=f"emailchange:{key}:{_minute_bucket()}"
        )

    async def confirm_email_change(self, *, token: str) -> None:
        row = await self.repository.consume_auth_token(token_hash=hash_token(token), purpose=EMAIL_CHANGE)
        user = await self.repository.get_user_by_id(row.user_id) if row else None
        if row is None or user is None or not user.is_active:
            raise InvalidAccountTokenError()
        taken = await self.repository.find_user_by_email_ci(row.email)
        if taken is not None and taken.id != user.id:
            raise EmailAlreadyExistsError(row.email)
        old_email = user.email
        user.email = row.email
        user.email_verified_at = datetime.now(UTC)  # the link arrived at the new address
        # Anything issued for the old address is now meaningless.
        await self.repository.invalidate_auth_tokens(user_id=user.id, purposes=(PASSWORD_RESET, EMAIL_VERIFICATION, EMAIL_CHANGE))
        await self.audit.log(actor_user_id=user.id, action="auth.email_changed", target_type="user", target_id=user.id)
        await self.jobs.enqueue(SEND_ACCOUNT_NOTICE, {"kind": "email_changed", "to": old_email, "new_email": user.email})


def token_expiry(purpose: str) -> datetime:
    from app.core.config import get_settings

    s = get_settings()
    delta = {
        PASSWORD_RESET: timedelta(minutes=s.password_reset_token_minutes),
        EMAIL_VERIFICATION: timedelta(hours=s.email_verification_token_hours),
        EMAIL_CHANGE: timedelta(minutes=s.email_change_token_minutes),
    }[purpose]
    return datetime.now(UTC) + delta
