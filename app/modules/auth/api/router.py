from typing import Annotated

from fastapi import APIRouter, Cookie, Request, Response, status

from app.core.rate_limit import limiter
from app.modules.auth.dependencies.auth import AuthServiceDep, CurrentUser
from app.modules.auth.schemas.auth import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserRead,
)
from app.modules.cart.dependencies.cart_deps import CartServiceDep
from app.modules.cart.dependencies.cart_identity import GUEST_CART_COOKIE

auth_router = APIRouter(prefix="/auth", tags=["auth"])


async def _merge_guest_cart_if_any(
    *,
    user_id,
    guest_cart_token: str | None,
    cart_service: CartServiceDep,
    response: Response,
) -> None:

    if not guest_cart_token:
        return
    await cart_service.merge_guest_cart_into_user(
        user_id=user_id, guest_token=guest_cart_token
    )
    response.delete_cookie(GUEST_CART_COOKIE)


@auth_router.post(
    "/register", response_model=UserRead, status_code=status.HTTP_201_CREATED
)
@limiter.limit("5/minute")
async def register(
    request: Request,
    response: Response,
    payload: RegisterRequest,
    service: AuthServiceDep,
    cart_service: CartServiceDep,
    guest_cart_token: Annotated[str | None, Cookie(alias=GUEST_CART_COOKIE)] = None,
) -> UserRead:
    user = await service.register(
        username=payload.username,
        email=payload.email,
        password=payload.password,
    )
    await _merge_guest_cart_if_any(
        user_id=user.id,
        guest_cart_token=guest_cart_token,
        cart_service=cart_service,
        response=response,
    )
    return UserRead.model_validate(user)


@auth_router.post("/login", response_model=TokenPair)
@limiter.limit("10/minute")
async def login(
    request: Request,
    response: Response,
    payload: LoginRequest,
    service: AuthServiceDep,
    cart_service: CartServiceDep,
    guest_cart_token: Annotated[str | None, Cookie(alias=GUEST_CART_COOKIE)] = None,
) -> TokenPair:
    tokens = await service.login(email=payload.email, password=payload.password)

    user = await service.get_user_by_email(payload.email)
    if user is not None:
        await _merge_guest_cart_if_any(
            user_id=user.id,
            guest_cart_token=guest_cart_token,
            cart_service=cart_service,
            response=response,
        )

    return tokens


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