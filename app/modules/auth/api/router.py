from app.core.rate_limit import limiter
from fastapi import APIRouter, Request, status

from app.modules.auth.dependencies.auth import AuthServiceDep, CurrentUser
from app.modules.auth.schemas.auth import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserRead,
)

auth_router = APIRouter(prefix="/auth", tags=["auth"])


@auth_router.post(
    "/register", response_model=UserRead, status_code=status.HTTP_201_CREATED
)
@limiter.limit("5/minute")
async def register(
    request: Request, payload: RegisterRequest, service: AuthServiceDep
) -> UserRead:
    user = await service.register(
        username=payload.username,
        email=payload.email,
        password=payload.password,
    )
    return UserRead.model_validate(user)


@auth_router.post("/login", response_model=TokenPair)
@limiter.limit("10/minute")
async def login(
    request: Request, payload: LoginRequest, service: AuthServiceDep
) -> TokenPair:
    return await service.login(email=payload.email, password=payload.password)


@auth_router.get("/me", response_model=UserRead)
async def get_me(current_user: CurrentUser) -> UserRead:
    return UserRead.model_validate(current_user)


@auth_router.post("/refresh", response_model=TokenPair)
@limiter.limit("20/minute")
async def refresh(
    request: Request, payload: RefreshRequest, service: AuthServiceDep
) -> TokenPair:
    return await service.refresh(refresh_token=payload.refresh_token)


@auth_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: RefreshRequest, service: AuthServiceDep) -> None:
    await service.logout(refresh_token=payload.refresh_token)
