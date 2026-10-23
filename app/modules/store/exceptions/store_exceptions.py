from app.core.exceptions import ConflictError


class StoreCurrencyLockedError(ConflictError):
    code = "store_currency_locked"

    def __init__(self) -> None:
        super().__init__(
            "The store currency is fixed once it has been set and cannot be changed. "
            "Orders keep the currency they were placed in."
        )
