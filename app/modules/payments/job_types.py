"""Job type names for the order/payment lifecycle (kept apart so services and handlers can share them)."""

SWEEP_STALE_ORDERS = "sweep_stale_orders"   # scheduled: finds unpaid orders past the timeout
EXPIRE_ORDER = "expire_order"               # one per stale order: cancel safely
EXECUTE_REFUND = "execute_refund"           # one per requested refund: talk to the provider
REPROCESS_DEFERRED_EVENTS = "reprocess_deferred_events"  # scheduled: bounded recovery of early webhook events


def expire_order_dedupe_key(order_id, bucket: int) -> str:
    """One expiry job per order per hour: a stuck order is retried each hour, never spammed."""
    return f"expire-order:{order_id}:{bucket}"


def execute_refund_dedupe_key(refund_id) -> str:
    return f"execute-refund:{refund_id}"
