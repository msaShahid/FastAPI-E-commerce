from typing import Annotated

from fastapi import APIRouter, Cookie, Request, Response, status

from app.core.rate_limit import limiter
from app.modules.auth.dependencies.auth import AccountServiceDep, AuthServiceDep, CurrentUser
from app.modules.auth.schemas.auth import (
    ChangeEmailRequest,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    MessageResponse,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenPair,
    TokenRequest,
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
    accounts: AccountServiceDep,
    cart_service: CartServiceDep,
    guest_cart_token: Annotated[str | None, Cookie(alias=GUEST_CART_COOKIE)] = None,
) -> UserRead:
    user = await service.register(
        username=payload.username,
        email=payload.email,
        password=payload.password,
    )
    await accounts.request_email_verification(user)  # a link to confirm the address; it never blocks signing in or buying
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


_RESET_SENT = MessageResponse(detail="If an account exists for that address, an email is on its way.")


@auth_router.post("/password/forgot", response_model=MessageResponse, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit("5/minute")
async def forgot_password(request: Request, payload: ForgotPasswordRequest, accounts: AccountServiceDep) -> MessageResponse:
    """Sends a reset link if the address belongs to an active account. The answer is identical either way."""
    await accounts.request_password_reset(payload.email)
    return _RESET_SENT


@auth_router.post("/password/reset", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/minute")
async def reset_password(request: Request, payload: ResetPasswordRequest, accounts: AccountServiceDep) -> None:
    """Sets a new password from an emailed link (works once). Signs the account out everywhere."""
    await accounts.reset_password(token=payload.token, new_password=payload.new_password)


@auth_router.post("/password/change", response_model=TokenPair)
@limiter.limit("10/minute")
async def change_password(
    request: Request, payload: ChangePasswordRequest, current_user: CurrentUser, accounts: AccountServiceDep
) -> TokenPair:
    """Needs the current password. Signs out every other device and returns a fresh token pair for this one."""
    return await accounts.change_password(
        user=current_user, current_password=payload.current_password, new_password=payload.new_password
    )


@auth_router.post("/email/verification", response_model=MessageResponse, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit("5/minute")
async def send_email_verification(request: Request, current_user: CurrentUser, accounts: AccountServiceDep) -> MessageResponse:
    """(Re)sends the confirmation link to the signed-in account's address. Nothing happens if it is already verified."""
    await accounts.request_email_verification(current_user)
    return MessageResponse(detail="If the address is not yet verified, an email is on its way.")


@auth_router.post("/email/verify", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("20/minute")
async def verify_email(request: Request, payload: TokenRequest, accounts: AccountServiceDep) -> None:
    await accounts.verify_email(token=payload.token)


@auth_router.post("/email/change", response_model=MessageResponse, status_code=status.HTTP_202_ACCEPTED)
@limiter.limit("5/minute")
async def change_email(
    request: Request, payload: ChangeEmailRequest, current_user: CurrentUser, accounts: AccountServiceDep
) -> MessageResponse:
    """Needs the current password. A confirmation link goes to the NEW address and a notice to the current one; nothing changes
    until the link is used. The answer does not reveal whether the new address is already taken."""
    await accounts.request_email_change(user=current_user, new_email=payload.new_email, password=payload.password)
    return MessageResponse(detail="If the address can be used, a confirmation email is on its way to it.")


@auth_router.post("/email/change/confirm", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("20/minute")
async def confirm_email_change(request: Request, payload: TokenRequest, accounts: AccountServiceDep) -> None:
    await accounts.confirm_email_change(token=payload.token)


@auth_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: RefreshRequest, service: AuthServiceDep) -> None:
    await service.logout(refresh_token=payload.refresh_token)