import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import jwt
from passlib.context import CryptContext

from app.core.config import get_settings

settings = get_settings()

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


class TokenError(Exception):
    """The ONE exception for an unusable token (bad signature, expired, malformed, missing claims)."""


class TokenType(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"


def hash_password(plain_password: str) -> str:
    """
    One-way hash. There is no corresponding decrypt_password function,
    deliberately -- that capability should never exist in this codebase.
    """
    return pwd_context.hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    """
    Re-hashes the candidate password with the same salt/parameters stored
    in `password_hash` and compares in constant time (passlib handles the
    constant-time comparison internally -- this matters, because a naive
    `==` string comparison can leak timing information about how many
    characters matched, in principle usable to guess a hash byte by byte).
    """
    return pwd_context.verify(plain_password, password_hash)


def _create_token(subject: str, token_type: TokenType, expires_delta: timedelta) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type.value,
        "iat": now,
        "exp": now + expires_delta,
    }
    if token_type == TokenType.REFRESH:
        # jti: a unique ID for THIS specific refresh token. We'll store
        # this in the database in the next step. Revoking a refresh token
        # later just means deleting the row with this jti -- the token
        # itself can't be "unissued", but we can make our server refuse
        # to honor it again.
        payload["jti"] = str(uuid.uuid4())

    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(subject: str) -> str:
    return _create_token(
        subject,
        TokenType.ACCESS,
        timedelta(minutes=settings.access_token_expire_minutes),
    )


def create_refresh_token(subject: str) -> str:
    return _create_token(
        subject,
        TokenType.REFRESH,
        timedelta(days=settings.refresh_token_expire_days),
    )


def decode_token(token: str) -> dict[str, Any]:
    """
    Verifies the signature AND checks expiration (PyJWT does both as part of jwt.decode, pinned to the configured algorithm,
    and `exp` and `sub` must be present). Raises TokenError on any problem: bad signature, expired, malformed. Callers catch
    this ONE exception type rather than needing to know the library's exception hierarchy.
    """
    try:
        return jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm], options={"require": ["exp", "sub"]}
        )
    except jwt.PyJWTError as exc:
        raise TokenError("Invalid or expired token") from exc


def unverified_claims(token: str) -> dict[str, Any]:
    """Claims of a token THIS server has just minted (to read its jti/exp for storage). Never use on a token a client sent."""
    return jwt.decode(token, options={"verify_signature": False, "verify_exp": False})