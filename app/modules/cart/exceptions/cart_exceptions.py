from app.core.exceptions import InvalidStateError, NotFoundError


class CartItemNotFoundError(NotFoundError):
    def __init__(self, item_id: int) -> None:
        super().__init__("Cart item not found")


class InsufficientStockError(InvalidStateError):
    def __init__(self, available: int) -> None:
        super().__init__(f"Only {available} unit(s) available in stock")


class ProductUnavailableError(InvalidStateError):
    def __init__(self) -> None:
        super().__init__("This product is no longer available")