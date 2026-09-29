import secrets
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Cookie, Depends, Response

from app.core.config import get_settings
from app.modules.auth.dependencies.auth import OptionalUser

GUEST_CART_COOKIE = "guest_cart_token"


@dataclass(frozen=True)
class CartOwner:

    user_id: UUID | None
    guest_token: str | None

    @property
    def is_guest(self) -> bool:
        return self.user_id is None


async def get_cart_owner(
    response: Response,
    current_user: OptionalUser,
    guest_cart_token: Annotated[str | None, Cookie(alias=GUEST_CART_COOKIE)] = None,
) -> CartOwner:
    """
    Resolves who a cart request is for. A logged-in caller always uses
    their own user_id (a guest cookie, if any, is left alone here --
    merging it into the account happens once, at login/register, not on
    every cart request). An anonymous caller gets an existing guest
    token from their cookie, or a fresh one that's set on the response
    the first time they touch the cart -- so add-to-cart-before-login
    works for a first-time visitor with no prior cookie at all.
    """
    if current_user is not None:
        return CartOwner(user_id=current_user.id, guest_token=None)

    token = guest_cart_token
    if not token:
        token = secrets.token_urlsafe(32)
        settings = get_settings()
        response.set_cookie(
            key=GUEST_CART_COOKIE,
            value=token,
            httponly=True,
            samesite="lax",
            secure=settings.environment == "production",
            max_age=settings.guest_cart_cookie_max_age_days * 24 * 60 * 60,
        )

    return CartOwner(user_id=None, guest_token=token)


CartOwnerDep = Annotated[CartOwner, Depends(get_cart_owner)]