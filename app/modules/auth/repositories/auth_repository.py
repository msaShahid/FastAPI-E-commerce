import uuid
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models.auth_token import AuthToken
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

    async def find_user_by_email_ci(self, email: str) -> User | None:
        """Exact match first, then case-insensitive (registration compares exactly, so look-ups for recovery are forgiving)."""
        user = await self.get_user_by_email(email)
        if user is not None:
            return user
        result = await self.db.execute(select(User).where(func.lower(User.email) == email.lower()).order_by(User.created_at).limit(1))
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

    async def get_refresh_token_by_jti(
        self, jti: str, *, for_update: bool = False
    ) -> RefreshToken | None:
        """
        for_update=True row-locks the token for the rest of the transaction, so two
        concurrent refreshes with the same token are serialized: the second waits,
        then sees it already revoked (i.e. a replay) instead of also succeeding.
        """
        query = select(RefreshToken).where(RefreshToken.jti == jti)
        if for_update:
            query = query.with_for_update().execution_options(populate_existing=True)
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------ emailed single-use tokens
    async def create_auth_token(self, *, user_id: uuid.UUID, purpose: str, token_hash: str, email: str, expires_at: datetime) -> None:
        self.db.add(AuthToken(user_id=user_id, purpose=purpose, token_hash=token_hash, email=email, expires_at=expires_at))
        await self.db.flush()

    async def consume_auth_token(self, *, token_hash: str, purpose: str):
        """Marks the token used and returns (user_id, email) in ONE statement, so two concurrent uses of one link cannot both
        succeed. None if it is unknown, of another purpose, already used, or expired."""
        result = await self.db.execute(
            update(AuthToken)
            .where(
                AuthToken.token_hash == token_hash, AuthToken.purpose == purpose,
                AuthToken.used_at.is_(None), AuthToken.expires_at > func.now(),
            )
            .values(used_at=func.now())
            .returning(AuthToken.user_id, AuthToken.email)
        )
        return result.one_or_none()

    async def invalidate_auth_tokens(self, *, user_id: uuid.UUID, purposes: tuple[str, ...]) -> None:
        await self.db.execute(
            update(AuthToken)
            .where(AuthToken.user_id == user_id, AuthToken.purpose.in_(purposes), AuthToken.used_at.is_(None))
            .values(used_at=func.now())
        )
        await self.db.flush()

    async def count_recent_auth_tokens(self, *, user_id: uuid.UUID, purpose: str, since: datetime) -> int:
        return (
            await self.db.execute(
                select(func.count()).select_from(AuthToken).where(
                    AuthToken.user_id == user_id, AuthToken.purpose == purpose, AuthToken.created_at >= since
                )
            )
        ).scalar_one()

    async def revoke_all_refresh_tokens(self, user_id: uuid.UUID) -> None:
        await self.db.execute(update(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked.is_(False)).values(revoked=True))
        await self.db.flush()

    async def commit(self) -> None:
        """
        Commits now. Used when a change must survive the request even though the
        request then fails (get_db rolls back on an exception), e.g. revoking a
        token family after a replay was detected.
        """
        await self.db.commit()

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
