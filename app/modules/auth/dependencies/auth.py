import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.database import DbSession
from app.core.logging_config import bind
from app.core.security import TokenError, TokenType, decode_token
from app.modules.auth.models.user import User
from app.modules.auth.repositories.auth_repository import AuthRepository
from app.modules.auth.services.auth_service import AuthService
from app.shared.enums.roles import UserRole

bearer_scheme = HTTPBearer()
optional_bearer_scheme = HTTPBearer(auto_error=False)


def get_auth_repository(db: DbSession) -> AuthRepository:
    return AuthRepository(db)


def get_auth_service(
    repository: Annotated[AuthRepository, Depends(get_auth_repository)],
) -> AuthService:
    return AuthService(repository)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer_scheme)],
    repository: Annotated[AuthRepository, Depends(get_auth_repository)],
) -> User:

    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = decode_token(credentials.credentials)
    except TokenError as exc:
        raise unauthorized from exc

    if payload.get("type") != TokenType.ACCESS.value:
        raise unauthorized

    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise unauthorized from exc

    user = await repository.get_user_by_id(user_id)
    if user is None or not user.is_active:
        raise unauthorized

    bind(user_id=str(user.id))  # every later log line in this request carries it
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_optional_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(optional_bearer_scheme)
    ],
    repository: Annotated[AuthRepository, Depends(get_auth_repository)],
) -> User | None:
    """
    For endpoints that are public but behave differently for a logged-in
    (specifically admin) caller -- e.g. the product catalog, where an
    admin should see drafts/archived items that anonymous or non-admin
    callers should not. No credentials, or invalid/expired ones, is
    treated the same as "anonymous", never as an error: this dependency
    must never turn a public endpoint into one that requires auth.
    """
    if credentials is None:
        return None
    try:
        return await get_current_user(credentials=credentials, repository=repository)
    except HTTPException:
        return None


OptionalUser = Annotated[User | None, Depends(get_optional_current_user)]


async def require_admin(current_user: CurrentUser) -> User:

    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires administrator privileges",
        )
    return current_user


AdminUser = Annotated[User, Depends(require_admin)]