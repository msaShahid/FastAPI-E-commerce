import enum


class TaxRecordStatus(enum.StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    RETIRED = "retired"


class ClassificationKind(enum.StrEnum):
    HSN = "hsn"
    SAC = "sac"


class TaxTreatment(enum.StrEnum):
    TAXABLE = "taxable"
    EXEMPT = "exempt"
    NIL_RATED = "nil_rated"
    NON_GST = "non_gst"
