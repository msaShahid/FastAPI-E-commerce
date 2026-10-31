from app.core.exceptions import ConflictError, InvalidStateError, NotFoundError


class CouponInvalidError(InvalidStateError):
    """One answer for a code that does not exist, is switched off, has not started or has ended: a customer learns nothing about codes they
    did not already have."""

    code = "coupon_invalid"

    def __init__(self) -> None:
        super().__init__("This coupon code is not valid")


class CouponMinimumNotMetError(InvalidStateError):
    code = "coupon_minimum_not_met"

    def __init__(self, minimum_cents: int) -> None:
        super().__init__("Your order does not reach the minimum for this coupon")
        self.minimum_cents = minimum_cents


class CouponLimitReachedError(InvalidStateError):
    code = "coupon_limit_reached"

    def __init__(self) -> None:
        super().__init__("This coupon has been used the maximum number of times")


class CouponNotApplicableError(InvalidStateError):
    code = "coupon_not_applicable"

    def __init__(self, reason: str = "This coupon does not apply to your order") -> None:
        super().__init__(reason)


class CouponSettingsInvalidError(InvalidStateError):
    """An update that would leave the coupon inconsistent (for example a fixed coupon without an amount)."""

    code = "coupon_settings_invalid"


class CouponNotFoundError(NotFoundError):
    code = "coupon_not_found"

    def __init__(self) -> None:
        super().__init__("Coupon not found")


class CouponCodeExistsError(ConflictError):
    code = "coupon_code_exists"

    def __init__(self) -> None:
        super().__init__("A coupon with this code already exists")


class CouponInUseError(ConflictError):
    code = "coupon_in_use"

    def __init__(self, what: str = "change that") -> None:
        super().__init__(f"This coupon has been used by orders, so you cannot {what}. Switch it off instead (active: false).")
