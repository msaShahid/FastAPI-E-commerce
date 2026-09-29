from datetime import UTC, datetime

from jose import JWTError, jwt

from app.core.security import (
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
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
        if user is None or not verify_password(password, user.password_hash):
            raise InvalidCredentialsError()

        if not user.is_active:
            raise InvalidCredentialsError()

        access_token = create_access_token(subject=str(user.id))
        refresh_token = create_refresh_token(subject=str(user.id))

        refresh_payload = jwt.get_unverified_claims(refresh_token)
        expires_at = datetime.fromtimestamp(refresh_payload["exp"], tz=UTC)

        await self.repository.store_refresh_token(
            user_id=user.id,
            jti=refresh_payload["jti"],
            expires_at=expires_at,
        )

        return TokenPair(access_token=access_token, refresh_token=refresh_token)

    async def refresh(self, *, refresh_token: str) -> TokenPair:

        try:
            payload = decode_token(refresh_token)
        except JWTError as exc:
            raise InvalidRefreshTokenError() from exc

        if payload.get("type") != TokenType.REFRESH.value:
            raise InvalidRefreshTokenError()

        jti = payload.get("jti")
        stored = await self.repository.get_refresh_token_by_jti(jti)

        if stored is None or stored.revoked or stored.expires_at < datetime.now(UTC):
            raise InvalidRefreshTokenError()

        user = await self.repository.get_user_by_id(stored.user_id)
        if user is None or not user.is_active:
            raise InvalidRefreshTokenError()

        # Rotation: revoke the one just used BEFORE issuing a new one.
        await self.repository.revoke_refresh_token(stored)

        subject = payload["sub"]
        new_access_token = create_access_token(subject=subject)
        new_refresh_token = create_refresh_token(subject=subject)

        new_payload = jwt.get_unverified_claims(new_refresh_token)
        await self.repository.store_refresh_token(
            user_id=stored.user_id,
            jti=new_payload["jti"],
            expires_at=datetime.fromtimestamp(new_payload["exp"], tz=UTC),
        )

        return TokenPair(access_token=new_access_token, refresh_token=new_refresh_token)

    async def get_user_by_email(self, email: str) -> User | None:
        return await self.repository.get_user_by_email(email)

    async def logout(self, *, refresh_token: str) -> None:
    
        try:
            payload = decode_token(refresh_token)
        except JWTError:
            return

        jti = payload.get("jti")
        if jti is None:
            return

        stored = await self.repository.get_refresh_token_by_jti(jti)
        if stored is not None and not stored.revoked:
            await self.repository.revoke_refresh_token(stored)