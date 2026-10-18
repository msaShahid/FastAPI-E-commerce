import enum


class TaxRecordStatus(enum.StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    RETIRED = "retired"


class ClassificationKind(enum.StrEnum):
    HSN = "hsn"
    SAC = "sac"


class ProductTaxState(enum.StrEnum):
    """Whether a product points at an approved classification. NOT a tax calculation.

    assigned_approved means only that a classification is assigned and approved; whether a
    rule applies on a given date is decided by the calculator (a later phase).
    """

    UNASSIGNED = "unassigned"
    ASSIGNED_UNAPPROVED = "assigned_unapproved"
    ASSIGNED_APPROVED = "assigned_approved"


def product_tax_state(classification_status: str | None) -> "ProductTaxState":
    if classification_status is None:
        return ProductTaxState.UNASSIGNED
    if classification_status == TaxRecordStatus.APPROVED:
        return ProductTaxState.ASSIGNED_APPROVED
    return ProductTaxState.ASSIGNED_UNAPPROVED


class PricingMode(enum.StrEnum):
    """Whether listed prices already include GST. Chosen explicitly per client; never defaulted."""

    INCLUSIVE = "inclusive"
    EXCLUSIVE = "exclusive"


class PlaceOfSupplyBasis(enum.StrEnum):
    """Which address decides the place of supply. Only the mode the calculator will implement."""

    DELIVERY_STATE = "delivery_state"


class ShippingTaxTreatment(enum.StrEnum):
    """How shipping enters the taxable value (an engine mode, NOT a legal ruling)."""

    NOT_TAXED = "not_taxed"
    TAXABLE_WITH_GOODS = "taxable_with_goods"


class RoundingPolicy(enum.StrEnum):
    LINE_HALF_UP = "line_half_up"
    INVOICE_HALF_UP = "invoice_half_up"


class TaxTreatment(enum.StrEnum):
    TAXABLE = "taxable"
    EXEMPT = "exempt"
    NIL_RATED = "nil_rated"
    NON_GST = "non_gst"
