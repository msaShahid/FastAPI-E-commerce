from app.core.exceptions import ConflictError, InvalidStateError, NotFoundError


class VariantNotFoundError(NotFoundError):
    code = "variant_not_found"

    def __init__(self) -> None:
        super().__init__("Variant not found")


class OptionNotFoundError(NotFoundError):
    code = "option_not_found"

    def __init__(self) -> None:
        super().__init__("Option or option value not found")


class InvalidVariantSpecError(InvalidStateError):
    """The request describes options or variants that cannot exist (incomplete combination, duplicates inside the request, unknown values)."""

    code = "invalid_variant_spec"


class OptionsLockedError(ConflictError):
    code = "options_locked"

    def __init__(self) -> None:
        super().__init__("This product already has variants, so its option sets cannot be redefined. Add values to an option instead.")


class ProductHasStockError(ConflictError):
    code = "product_has_stock"

    def __init__(self) -> None:
        super().__init__("This product holds stock of its own. Use POST /products/{id}/convert-to-variants to turn it into a product with options.")


class ProductInUseError(ConflictError):
    code = "product_in_use"

    def __init__(self) -> None:
        super().__init__("This product is already in carts or orders. Use POST /products/{id}/convert-to-variants to turn it into a product with options.")


class VariantCombinationExistsError(ConflictError):
    code = "variant_combination_exists"

    def __init__(self, detail: str = "That combination already exists") -> None:
        super().__init__(detail)


class ProductNeedsActiveVariantError(ConflictError):
    code = "product_needs_active_variant"

    def __init__(self) -> None:
        super().__init__("An active product with options needs at least one active variant")


class UseVariantStockError(InvalidStateError):
    code = "use_variant_stock"

    def __init__(self) -> None:
        super().__init__("This product has options: its stock lives on its variants. Adjust a variant's stock instead.")


class StockWouldBeNegativeError(InvalidStateError):
    code = "stock_would_be_negative"

    def __init__(self, current: int) -> None:
        super().__init__(f"That change would take stock below zero (current stock: {current})")


class VariantInUseError(ConflictError):
    code = "variant_in_use"

    def __init__(self) -> None:
        super().__init__("This variant has stock history, or is in a cart or an order, so it cannot be deleted. Set it to inactive instead.")


class OptionValueInUseError(ConflictError):
    code = "option_value_in_use"

    def __init__(self) -> None:
        super().__init__("A variant uses this value, so it cannot be removed")


class AlreadyHasVariantsError(ConflictError):
    code = "already_has_variants"

    def __init__(self) -> None:
        super().__init__("This product already has options")


class StockChangedError(ConflictError):
    """The product's stock is not what the administrator allocated against: nothing was changed, review and send again."""

    code = "stock_changed"

    def __init__(self, current: int) -> None:
        super().__init__(f"The product's stock is now {current}, not the amount you allocated. Nothing was changed.")
        self.current_stock = current


class StockAllocationMismatchError(InvalidStateError):
    code = "stock_allocation_mismatch"

    def __init__(self, allocated: int, stock: int) -> None:
        super().__init__(f"The variants' stock adds up to {allocated} but the product has {stock}: stock must be allocated exactly, none lost or invented")


class ProductHasOpenOrdersError(ConflictError):
    code = "product_has_open_orders"

    def __init__(self, count: int) -> None:
        super().__init__(
            f"{count} unpaid or paid-but-unshipped order(s) contain this product, and their stock could not be returned to a variant. "
            "Finish, ship or cancel them first."
        )


class ProductNotConvertibleError(InvalidStateError):
    code = "product_not_convertible"


class SkuInUseError(ConflictError):
    code = "sku_already_exists"

    def __init__(self, skus: list[str]) -> None:
        super().__init__("SKU already in use by a product or another variant: " + ", ".join(sorted(skus)))
