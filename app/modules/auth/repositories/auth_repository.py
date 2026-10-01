import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models.refresh_token import RefreshToken
from app.modules.auth.models.user import User


class AuthRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_user_by_id(self, user_id: uuid.UUID) -> User | None:
        return await self.db.get(User, user_id)

    async def get_user_by_email(self, email: str) -> User | None:
        result = await self.db.execute(select(User).where(User.email == email))
        return result.scalar_one_or_none()

    async def get_user_by_username(self, username: str) -> User | None:
        result = await self.db.execute(select(User).where(User.username == username))
        return result.scalar_one_or_none()

    async def create_user(
        self, *, username: str, email: str, password_hash: str
    ) -> User:
        user = User(username=username, email=email, password_hash=password_hash)
        self.db.add(user)
        await self.db.flush()
        return user

    async def store_refresh_token(
        self,
        *,
        user_id: uuid.UUID,
        jti: str,
        expires_at: datetime,
        family_id: uuid.UUID | None = None,
    ) -> RefreshToken:
        # No family_id means "this is a brand-new login", so it starts a
        # brand-new family. A rotation (see AuthService.refresh) passes
        # the previous token's family_id along instead, so the whole
        # chain of tokens from one login shares one family_id.
        token = RefreshToken(
            user_id=user_id,
            jti=jti,
            expires_at=expires_at,
            family_id=family_id or uuid.uuid4(),
        )
        self.db.add(token)
        await self.db.flush()
        return token

    async def get_refresh_token_by_jti(self, jti: str) -> RefreshToken | None:
        result = await self.db.execute(
            select(RefreshToken).where(RefreshToken.jti == jti)
        )
        return result.scalar_one_or_none()

    async def revoke_refresh_token(self, token: RefreshToken) -> None:
        token.revoked = True
        await self.db.flush()

    async def revoke_family(self, family_id: uuid.UUID) -> None:

        await self.db.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked.is_(False))
            .values(revoked=True)
        )
        await self.db.flush()
