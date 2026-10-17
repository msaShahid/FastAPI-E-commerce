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


class TaxTreatment(enum.StrEnum):
    TAXABLE = "taxable"
    EXEMPT = "exempt"
    NIL_RATED = "nil_rated"
    NON_GST = "non_gst"
