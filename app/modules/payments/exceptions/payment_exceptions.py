from app.core.exceptions import InvalidStateError, NotFoundError, ServiceUnavailableError


class PaymentNotFoundError(NotFoundError):
    def __init__(self) -> None:
        super().__init__("Payment not found")


class OrderNotPayableError(InvalidStateError):
    def __init__(self, message: str = "This order cannot be paid") -> None:
        super().__init__(message)


class OrderNotRefundableError(InvalidStateError):
    def __init__(self, message: str = "This order cannot be refunded") -> None:
        super().__init__(message)


class LivePaymentsDisabledError(ServiceUnavailableError):
    code = "live_payments_disabled"

    def __init__(self) -> None:
        super().__init__("Live payments are disabled")
