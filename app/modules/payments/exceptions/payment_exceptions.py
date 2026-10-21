from app.core.exceptions import (
    ConflictError,
    InvalidStateError,
    NotFoundError,
    ServiceUnavailableError,
)


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



class OrderCurrencyUnknownError(InvalidStateError):
    """An order created before currencies were recorded: no payment or refund until its currency is established."""

    code = "order_currency_unknown"

    def __init__(self) -> None:
        super().__init__(
            "This order has no recorded currency, so it cannot be refunded or paid. An administrator must "
            "establish its currency from reliable records first (GET /admin/orders/unknown-currency)."
        )


class PartialRefundNotSupportedError(InvalidStateError):
    """NovaCart initiates FULL refunds only. A partial refund that already exists (made at the provider) is recorded,
    but the remainder is completed where it was started."""

    code = "partial_refund_not_supported"

    def __init__(self) -> None:
        super().__init__(
            "Part of this payment has already been refunded at the provider. NovaCart initiates full refunds only: "
            "complete the remaining refund in the provider's dashboard, where it will be recorded here."
        )


class RefundAlreadyRequestedError(ConflictError):
    code = "refund_already_requested"

    def __init__(self, cancelling: bool) -> None:
        super().__init__(
            "A refund for this payment is already in progress"
            + (" without cancelling the order" if not cancelling else " as part of a cancellation")
        )
