"""Delivery eligibility: may this store deliver to this address?

Today NO check is made (a store decision for the first release): every address is accepted, and each order records that fact in its
shipping snapshot (`delivery_check: {"checked": false, "policy": "none"}`), so it is always clear later which orders were never
checked. The decision is made here, on the backend, before any stock is touched; a storefront can never override it.

This is the seam for a later check. A new policy (an administrator-managed, validated list of serviceable pincodes, or a shipping-provider
lookup) implements `DeliveryEligibility`, is returned by `get_delivery_eligibility()`, and checkout starts refusing undeliverable
addresses with the `delivery_not_available` error: nothing else in checkout changes."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DeliveryDecision:
    deliverable: bool
    checked: bool                   # False: no check was made (the address was accepted by policy, not verified)
    policy: str                     # which policy decided, recorded on the order
    reason: str | None = None       # a short fixed code when not deliverable, e.g. "pincode_not_serviceable"

    def as_snapshot(self) -> dict:
        return {"checked": self.checked, "policy": self.policy}


class DeliveryEligibility(Protocol):
    policy: str

    async def check(self, address: dict) -> DeliveryDecision:
        """`address` has the order's shipping_* column names (shipping_postal_code, shipping_state, shipping_country, ...)."""
        ...


class NoServiceabilityCheck:
    policy = "none"

    async def check(self, address: dict) -> DeliveryDecision:
        return DeliveryDecision(deliverable=True, checked=False, policy=self.policy)


def get_delivery_eligibility() -> DeliveryEligibility:
    return NoServiceabilityCheck()
