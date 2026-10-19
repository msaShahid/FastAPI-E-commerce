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



class PaymentInconsistentError(InvalidStateError):
    """An existing payment/order disagree on amount or currency: nothing is created or changed."""

    code = "payment_inconsistent"

    def __init__(self, message: str = "The payment does not match the order") -> None:
        super().__init__(message)



class ReconciliationNotOpenError(InvalidStateError):
    code = "reconciliation_not_open"

    def __init__(self) -> None:
        super().__init__("This reconciliation record does not exist or is already resolved")



class PaymentDisputedError(InvalidStateError):
    """A payment with an open dispute cannot be refunded here: the dispute must be resolved first."""

    code = "payment_disputed"

    def __init__(self) -> None:
        super().__init__("This payment has an open dispute and cannot be refunded until it is resolved")
