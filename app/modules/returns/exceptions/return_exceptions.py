from app.core.exceptions import ConflictError, InvalidStateError, NotFoundError


class ReturnNotFoundError(NotFoundError):
    code = "return_not_found"

    def __init__(self) -> None:
        super().__init__("Return not found")


class ReturnsDisabledError(InvalidStateError):
    code = "returns_disabled"

    def __init__(self) -> None:
        super().__init__("This store does not accept online return requests. Please contact the store.")


class OrderNotShippedError(InvalidStateError):
    code = "order_not_shipped"

    def __init__(self) -> None:
        super().__init__("Only an order that has been shipped can be returned")


class ReturnWindowClosedError(InvalidStateError):
    code = "return_window_closed"

    def __init__(self, days: int) -> None:
        super().__init__(f"The {days}-day return period for this order has ended")


class NothingReturnableError(InvalidStateError):
    code = "nothing_returnable"

    def __init__(self) -> None:
        super().__init__("Everything in this order is already part of a return request")


class ReturnItemInvalidError(InvalidStateError):
    code = "return_item_invalid"


class ReturnQuantityExceededError(InvalidStateError):
    code = "return_quantity_exceeded"

    def __init__(self, returnable: int) -> None:
        super().__init__(f"You can return at most {returnable} of this item")


class ReturnStateError(ConflictError):
    code = "return_invalid_state"

    def __init__(self, status: str, action: str) -> None:
        super().__init__(f"This return is {status}, so it cannot be {action}")


class ReturnNotFullError(ConflictError):
    code = "return_not_full"

    def __init__(self) -> None:
        super().__init__(
            "NovaCart refunds a whole order at a time, and this return does not bring back every unit of it. Close the return with the resolution "
            "'refunded_outside' once you have refunded the customer at your payment provider, or 'replacement_sent' / 'no_refund'."
        )


class ReturnReceiptInvalidError(InvalidStateError):
    code = "return_receipt_invalid"
