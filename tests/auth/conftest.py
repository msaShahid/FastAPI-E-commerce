import uuid
from dataclasses import dataclass, field
from datetime import datetime

import pytest

from app.modules.auth.models.user import User
from app.shared.enums.roles import UserRole


@dataclass
class FakeRefreshToken:

    jti: str
    user_id: uuid.UUID
    expires_at: datetime
    family_id: uuid.UUID = field(default_factory=uuid.uuid4)
    revoked: bool = False


class FakeAuthRepository:

    def __init__(self) -> None:
        self.users_by_id: dict[uuid.UUID, User] = {}
        self.users_by_email: dict[str, User] = {}
        self.users_by_username: dict[str, User] = {}
        self.refresh_tokens: dict[str, FakeRefreshToken] = {}

    async def get_user_by_id(self, user_id: uuid.UUID) -> User | None:
        return self.users_by_id.get(user_id)

    async def get_user_by_email(self, email: str) -> User | None:
        return self.users_by_email.get(email)

    async def get_user_by_username(self, username: str) -> User | None:
        return self.users_by_username.get(username)

    async def create_user(
        self, *, username: str, email: str, password_hash: str
    ) -> User:
        user = User(
            id=uuid.uuid4(),
            username=username,
            email=email,
            password_hash=password_hash,
            role=UserRole.USER,
            is_active=True,
        )
        self.users_by_id[user.id] = user
        self.users_by_email[email] = user
        self.users_by_username[username] = user
        return user

    async def store_refresh_token(
        self,
        *,
        user_id: uuid.UUID,
        jti: str,
        expires_at: datetime,
        family_id: uuid.UUID | None = None,
    ) -> FakeRefreshToken:
        token = FakeRefreshToken(
            jti=jti,
            user_id=user_id,
            expires_at=expires_at,
            family_id=family_id or uuid.uuid4(),
        )
        self.refresh_tokens[jti] = token
        return token

    async def get_refresh_token_by_jti(self, jti: str) -> FakeRefreshToken | None:
        return self.refresh_tokens.get(jti)

    async def revoke_refresh_token(self, token: FakeRefreshToken) -> None:
        token.revoked = True

    async def revoke_family(self, family_id: uuid.UUID) -> None:
        for token in self.refresh_tokens.values():
            if token.family_id == family_id:
                token.revoked = True


@pytest.fixture
def fake_repository() -> FakeAuthRepository:
    """A fresh, empty fake repository for each test -- no shared state leaks between tests."""
    return FakeAuthRepository()


@pytest.fixture
def make_user():

    def _make_user(**overrides) -> User:
        defaults = dict(
            id=uuid.uuid4(),
            username="testuser",
            email="testuser@example.com",
            password_hash="irrelevant-for-these-tests",
            role=UserRole.USER,
            is_active=True,
        )
        defaults.update(overrides)
        return User(**defaults)

    return _make_user
