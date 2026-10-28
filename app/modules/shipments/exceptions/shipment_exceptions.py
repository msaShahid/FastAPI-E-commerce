from app.core.exceptions import ConflictError, NotFoundError


class ShipmentNotFoundError(NotFoundError):
    code = "shipment_not_found"

    def __init__(self) -> None:
        super().__init__("Shipment not found")


class OrderNotShippableError(ConflictError):
    code = "order_not_shippable"

    def __init__(self, status: str) -> None:
        super().__init__(f"A shipment can only be recorded for a paid or fulfilled order (this order is {status})")


class DuplicateTrackingError(ConflictError):
    code = "duplicate_tracking_number"

    def __init__(self) -> None:
        super().__init__("This tracking number is already recorded for this order and carrier")


class InvalidShipmentStatusChangeError(ConflictError):
    code = "invalid_shipment_status_change"

    def __init__(self, current: str, new: str) -> None:
        super().__init__(f"A shipment cannot change from {current} to {new}")
