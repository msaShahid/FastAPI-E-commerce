from app.core.exceptions import ConflictError, InvalidStateError, NotFoundError


class CartItemNotFoundError(NotFoundError):
    def __init__(self, item_id: int) -> None:
        super().__init__("Cart item not found")


class InsufficientStockError(InvalidStateError):
    """Exact stock is not public, so this names a number only when stock is at or below the low-stock display threshold."""

    def __init__(self, available: int) -> None:
        from app.core.config import get_settings

        settings = get_settings()
        show = available <= settings.low_stock_display_threshold or settings.public_stock_visibility == "exact"
        super().__init__(f"Only {available} unit(s) available in stock" if show else "Not enough stock for that quantity")


class ProductUnavailableError(InvalidStateError):
    def __init__(self) -> None:
        super().__init__("This product is no longer available")


class CartFullError(ConflictError):
    def __init__(self, limit: int) -> None:
        super().__init__(
            f"A cart can hold at most {limit} different products. "
            "Remove one, or check out, before adding another."
        )


class VariantRequiredError(InvalidStateError):
    """The product has options (size, colour, ...): one specific variant must be chosen."""

    code = "variant_required"

    def __init__(self) -> None:
        super().__init__("This product comes in options: choose a variant (send variant_id)")


class VariantUnavailableError(InvalidStateError):
    """One answer for every way a variant cannot be bought: unknown, another product's, inactive, or its product is not for sale."""

    code = "variant_unavailable"

    def __init__(self) -> None:
        super().__init__("This option is not available")
