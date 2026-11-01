from app.core.exceptions import ConflictError, InvalidStateError, NotFoundError


class DemoOnlyTestDetailsError(InvalidStateError):
    """The demo gateway accepts ONLY its published fake test details. Nothing else is accepted, recorded or echoed back."""

    code = "demo_only_test_details"

    def __init__(self) -> None:
        super().__init__("This is the demo gateway: only the listed test details are accepted. Never enter real card or account details here.")


class NoDemoPaymentError(NotFoundError):
    code = "no_demo_payment"

    def __init__(self) -> None:
        super().__init__("There is no open demo payment for this order")


class NotADemoPaymentError(ConflictError):
    code = "not_a_demo_payment"

    def __init__(self) -> None:
        super().__init__("This order's payment was not started with the demo gateway")


class DemoGatewayInactiveError(ConflictError):
    code = "demo_gateway_inactive"

    def __init__(self) -> None:
        super().__init__("The demo gateway is not active on this store")
