from app.core.exceptions import ConflictError, ForbiddenError, InvalidStateError, NotFoundError


class _Coded:
    code: str = "tax_error"


class TaxRecordNotFoundError(_Coded, NotFoundError):
    code = "tax_record_not_found"

    def __init__(self, what: str) -> None:
        super().__init__(f"{what} not found")


class DuplicateClassificationError(_Coded, ConflictError):
    code = "classification_exists"

    def __init__(self) -> None:
        super().__init__("A classification with this kind and code already exists")


class DuplicateRegistrationError(_Coded, ConflictError):
    code = "registration_exists"

    def __init__(self) -> None:
        super().__init__("A registration with this GSTIN already exists")


class InvalidTaxTransitionError(_Coded, ConflictError):
    code = "tax_master_invalid_transition"

    def __init__(self, action: str, status: str) -> None:
        super().__init__(f"Cannot {action} a record in status '{status}'")


class ImmutableTaxRecordError(_Coded, ConflictError):
    code = "tax_master_immutable"

    def __init__(self) -> None:
        super().__init__(
            "Only draft records can be edited. Approved records are closed or retired "
            "and replaced by a new record."
        )


class ApprovalRequiresDistinctApproverError(_Coded, ForbiddenError):
    code = "approval_requires_distinct_approver"

    def __init__(self) -> None:
        super().__init__("A record cannot be approved by the administrator who created it")


class SelfApprovalNoteRequiredError(_Coded, InvalidStateError):
    code = "self_approval_note_required"

    def __init__(self) -> None:
        super().__init__("Self-approval requires a written note")


class ApprovalPrerequisiteError(_Coded, InvalidStateError):
    code = "approval_prerequisite_missing"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class RuleOverlapError(_Coded, ConflictError):
    code = "rule_overlap"

    def __init__(self) -> None:
        super().__init__("An approved rule already applies to this classification in that period")


class ClassificationInUseError(_Coded, ConflictError):
    code = "classification_in_use"

    def __init__(self, reason: str = "it still has draft or approved rules") -> None:
        super().__init__(f"This classification cannot be retired: {reason}")


class InvalidTaxInputError(_Coded, InvalidStateError):
    code = "tax_master_invalid_input"
