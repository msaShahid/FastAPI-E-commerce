import enum


class RefundStatus(enum.StrEnum):
    """requested = we asked; pending = the provider accepted but has not finished; succeeded and
    failed are provider-CONFIRMED and final; uncertain = we could not learn the outcome (needs a human)."""

    REQUESTED = "requested"
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


# At most one refund per payment may be in any of these states (database unique index).
ACTIVE_REFUND_STATUSES = (
    RefundStatus.REQUESTED,
    RefundStatus.PENDING,
    RefundStatus.SUCCEEDED,
    RefundStatus.UNCERTAIN,
)


class ReconciliationKind(enum.StrEnum):
    LATE_PAYMENT = "late_payment"                    # paid, but the order can no longer be paid
    UNMATCHED_PAYMENT = "unmatched_payment"          # a payment we have no record of
    AMOUNT_MISMATCH = "amount_mismatch"              # provider amount/currency differ from ours
    UNVERIFIABLE_PAYMENT = "unverifiable_payment"    # the event carried no amount/currency
    ORDER_MISMATCH = "order_mismatch"                # payment and order disagree
    CANCEL_UNCERTAIN = "cancel_uncertain"            # could not confirm the intent is no longer payable
    CANCEL_PAYMENT_SUCCEEDED = "cancel_payment_succeeded"  # the provider reported success while we were cancelling
    REFUND_UNCERTAIN = "refund_uncertain"            # could not learn the refund outcome
    REFUND_MISMATCH = "refund_mismatch"              # refunded amount/currency differ from the request
    EXTERNAL_REFUND = "external_refund"              # a refund made at the provider, recorded here
    DISPUTE_OPENED = "dispute_opened"                # a chargeback needs a response
    DISPUTE_LOST = "dispute_lost"                    # the dispute was lost: money left the account
    UNMATCHED_DISPUTE = "unmatched_dispute"          # a dispute for a payment we have no record of


class ReconciliationStatus(enum.StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class ReconciliationResolution(enum.StrEnum):
    REFUND_REQUESTED = "refund_requested"
    HANDLED_EXTERNALLY = "handled_externally"        # e.g. refunded in the provider dashboard
    NO_ACTION_NEEDED = "no_action_needed"
