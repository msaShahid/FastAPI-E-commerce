from app.core.exceptions import InvalidStateError


class InvalidReportRangeError(InvalidStateError):
    code = "invalid_report_range"


class ReportCurrencyUnknownError(InvalidStateError):
    code = "store_currency_not_set"

    def __init__(self) -> None:
        super().__init__("The store currency is not set, so money totals cannot be reported. Set it first (PUT /store/currency).")
