from app.core.exceptions import (
    ConflictError,
    ForbiddenError,
    InvalidStateError,
    NotFoundError,
    ServiceUnavailableError,
)


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


class IdempotencyKeyConflictError(ConflictError):
    code = "idempotency_key_reused"

    def __init__(self) -> None:
        super().__init__(
            "This Idempotency-Key was already used with a different request. "
            "Use a new key for a different checkout."
        )


class PriceChangedError(ConflictError):
    """Raised when a cart line's price no longer matches the product's current price.

    Carries what the customer needs to review. The router turns it into a 409
    response with code "price_changed" (and commits the refreshed cart prices).
    """

    def __init__(self, changes: list[dict], new_subtotal_cents: int) -> None:
        super().__init__("Prices changed since you added these items. Review the new total.")
        self.changes = changes
        self.new_subtotal_cents = new_subtotal_cents


class CheckoutNotConfiguredError(ServiceUnavailableError):
    """Shipping or tax settings are missing, so a total cannot be computed safely."""

    code = "checkout_not_configured"

    def __init__(self) -> None:
        super().__init__(
            "Checkout is temporarily unavailable: shipping and tax are not configured."
        )


class ShippingAddressRequiredError(InvalidStateError):
    def __init__(self) -> None:
        super().__init__(
            "A shipping address is required to check out -- pass either "
            "address_id (a saved address) or shipping_address"
        )


class GuestEmailRequiredError(InvalidStateError):
    def __init__(self) -> None:
        super().__init__("An email address is required for guest checkout")



class ProductTaxUnassignedError(InvalidStateError):
    code = "product_tax_unassigned"

    def __init__(self, product_ids: list[int]) -> None:
        ids = ", ".join(str(i) for i in sorted(product_ids))
        super().__init__(
            f"These items cannot be ordered yet because their tax classification is not approved: product {ids}"
        )
        self.product_ids = sorted(product_ids)



class StatusChangeNotAllowedError(ConflictError):
    """An administrator asked for a status change that would bypass payment, stock or refund handling."""

    code = "status_change_not_allowed"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class CancellationNotAllowedError(ConflictError):
    """Only an unpaid (pending) order can be cancelled. A paid order needs a refund first, and the
    refund/cancellation policy for paid orders is a business decision that has not been made."""

    code = "cancellation_not_allowed"

    def __init__(self, status: str) -> None:
        super().__init__(
            f"An order in status '{status}' cannot be cancelled. "
            "Paid orders are handled through a refund, which an administrator requests."
        )


class CancellationUnconfirmedError(ConflictError):
    """The payment provider could not confirm that the payment can no longer be completed, so the
    order is NOT cancelled and its stock stays reserved. It is retried and reconciled."""

    code = "cancellation_unconfirmed"

    def __init__(self) -> None:
        super().__init__(
            "The order could not be cancelled yet because the payment provider could not confirm the "
            "payment state. Nothing was changed; this is being reconciled. Try again shortly."
        )


class PaymentInProgressError(ConflictError):
    code = "payment_in_progress"

    def __init__(self) -> None:
        super().__init__("A payment for this order is already in progress or completed, so it cannot be cancelled")
