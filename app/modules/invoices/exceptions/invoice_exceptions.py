from app.core.exceptions import InvalidStateError, NotFoundError


class InvoiceNotFoundError(NotFoundError):
    code = "invoice_not_found"

    def __init__(self) -> None:
        super().__init__("No invoice has been issued for this order")


class BusinessProfileIncompleteError(InvalidStateError):
    code = "business_profile_incomplete"

    def __init__(self) -> None:
        super().__init__("A GST invoice cannot be issued until the business profile (address, state, invoice prefix) is set: PUT /admin/business-profile")


class InvoiceNotApplicableError(InvalidStateError):
    """Only an order whose tax was calculated by the GST engine, and that has been paid, gets a tax invoice."""

    code = "invoice_not_applicable"
