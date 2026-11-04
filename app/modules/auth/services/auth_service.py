import logging
from datetime import UTC, datetime

from app.core.config import get_settings
from app.core.security import (
    TokenError,
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    unverified_claims,
    verify_against_decoy,
    verify_password,
)
from app.modules.auth.exceptions.auth_exceptions import (
    EmailAlreadyExistsError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    UsernameAlreadyExistsError,
)
from app.modules.auth.models.user import User
from app.modules.auth.repositories.auth_repository import AuthRepository
from app.modules.auth.schemas.auth import TokenPair

logger = logging.getLogger(__name__)

class AuthService:
    def __init__(self, repository: AuthRepository) -> None:
        self.repository = repository

    async def register(self, *, username: str, email: str, password: str) -> User:

        if await self.repository.get_user_by_email(email) is not None:
            raise EmailAlreadyExistsError(email)

        if await self.repository.get_user_by_username(username) is not None:
            raise UsernameAlreadyExistsError(username)

        password_hash = hash_password(password)
        return await self.repository.create_user(
            username=username,
            email=email,
            password_hash=password_hash,
        )

    async def login(self, *, email: str, password: str) -> TokenPair:

        user = await self.repository.get_user_by_email(email)
        if user is None:
            verify_against_decoy(password)  # same cost as a real check: timing must not reveal whether the email exists
            raise InvalidCredentialsError()
        if not verify_password(password, user.password_hash):
            raise InvalidCredentialsError()

        if not user.is_active:
            raise InvalidCredentialsError()

        return await self.issue_tokens(user)

    async def issue_tokens(self, user: User) -> TokenPair:
        """A fresh access + refresh pair for `user` (a new token family). Used by login and after a password change."""
        access_token = create_access_token(subject=str(user.id))
        refresh_token = create_refresh_token(subject=str(user.id))

        refresh_payload = unverified_claims(refresh_token)
        expires_at = datetime.fromtimestamp(refresh_payload["exp"], tz=UTC)

        await self.repository.store_refresh_token(
            user_id=user.id,
            jti=refresh_payload["jti"],
            expires_at=expires_at,
        )

        return TokenPair(access_token=access_token, refresh_token=refresh_token)

    async def _is_benign_replay(self, stored, now: datetime) -> bool:
        """Is this revoked token a token that was rotated a moment ago and presented again by a second tab, a retry or another
        instance (benign), rather than reuse of a stolen token? Only a REAL rotation inside the grace window counts, only a limited
        number of times, and never once the family itself has been shut down (logout, password change, earlier reuse detection)."""
        settings = get_settings()
        if settings.refresh_reuse_grace_seconds <= 0:
            return False
        rotated_at = getattr(stored, "rotated_at", None)
        if rotated_at is None or (now - rotated_at).total_seconds() > settings.refresh_reuse_grace_seconds:
            return False
        if (getattr(stored, "grace_replays", 0) or 0) >= settings.refresh_reuse_grace_max_replays:
            return False
        return await self.repository.family_has_active_token(stored.family_id)

    async def refresh(self, *, refresh_token: str, audit=None) -> TokenPair:
        """Exchange a refresh token for a new pair (rotation). `audit` (an AuditService) records a benign replay when given."""

        try:
            payload = decode_token(refresh_token)
        except TokenError as exc:
            raise InvalidRefreshTokenError() from exc

        if payload.get("type") != TokenType.REFRESH.value:
            raise InvalidRefreshTokenError()

        jti = payload.get("jti")
        stored = await self.repository.get_refresh_token_by_jti(jti, for_update=True)

        if stored is None:
            raise InvalidRefreshTokenError()

        now = datetime.now(UTC)
        replay = False
        if stored.revoked:
            if await self._is_benign_replay(stored, now):
                replay = True
            else:
                logger.warning(
                    "Refresh token reuse detected for jti=%s (family_id=%s, user_id=%s); "
                    "revoking entire token family.",
                    jti,
                    stored.family_id,
                    stored.user_id,
                )
                await self.repository.revoke_family(stored.family_id)
                # Persist the revocation NOW: the request fails below, and get_db
                # rolls back on an exception, which would silently undo it.
                await self.repository.commit()
                raise InvalidRefreshTokenError()

        if stored.expires_at < now:
            raise InvalidRefreshTokenError()

        user = await self.repository.get_user_by_id(stored.user_id)
        if user is None or not user.is_active:
            raise InvalidRefreshTokenError()

        if replay:
            # A benign race: leave the family as it is and hand this caller its OWN new token in the same family.
            await self.repository.record_grace_replay(stored)
            logger.info("Refresh token replay inside the grace window accepted (jti=%s, family_id=%s)", jti, stored.family_id)
            if audit is not None:
                await audit.log(
                    actor_user_id=stored.user_id,
                    action="auth.refresh_replay_accepted",
                    target_type="refresh_token",
                    target_id=stored.jti,
                    details={"family_id": str(stored.family_id), "replays": stored.grace_replays},
                )
        else:
            # Rotation: revoke the one just used BEFORE issuing a new one.
            await self.repository.rotate_refresh_token(stored, now=now)

        subject = payload["sub"]
        new_access_token = create_access_token(subject=subject)
        new_refresh_token = create_refresh_token(subject=subject)

        new_payload = unverified_claims(new_refresh_token)
        await self.repository.store_refresh_token(
            user_id=stored.user_id,
            jti=new_payload["jti"],
            expires_at=datetime.fromtimestamp(new_payload["exp"], tz=UTC),
            family_id=stored.family_id,
        )

        return TokenPair(access_token=new_access_token, refresh_token=new_refresh_token)

    async def get_user_by_email(self, email: str) -> User | None:
        return await self.repository.get_user_by_email(email)

    async def logout(self, *, refresh_token: str) -> None:

        try:
            payload = decode_token(refresh_token)
        except TokenError:
            return

        jti = payload.get("jti")
        if jti is None:
            return

        stored = await self.repository.get_refresh_token_by_jti(jti)
        if stored is not None and not stored.revoked:
            await self.repository.revoke_refresh_token(stored)
