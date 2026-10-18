from app.core.exceptions import ConflictError


class StoreCurrencyLockedError(ConflictError):
    code = "store_currency_locked"

    def __init__(self) -> None:
        super().__init__(
            "The store currency cannot be changed because orders already exist. "
            "Historical orders keep the currency they were placed in."
        )
