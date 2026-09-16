from app.core.exceptions import ForbiddenError, InvalidStateError, NotFoundError


class EmptyCartError(InvalidStateError):
    def __init__(self) -> None:
        super().__init__("Cannot check out an empty cart")


class OrderNotFoundError(NotFoundError):
    def __init__(self, order_id) -> None:
        super().__init__("Order not found")


class InvalidStatusTransitionError(InvalidStateError):
    def __init__(self, from_status: str, to_status: str) -> None:
        super().__init__(f"Cannot transition order from {from_status} to {to_status}")


class OrderAccessForbiddenError(ForbiddenError):
    def __init__(self) -> None:
        super().__init__("You do not have access to this order")