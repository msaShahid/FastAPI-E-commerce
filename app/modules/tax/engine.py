"""
Tax-engine integration boundary (Phase 2.4). NO GST is calculated here.

Checkout asks a TaxEngine for the tax on a cart. The contract is deliberately strict:
an engine either returns a TaxResult it stands behind, or raises. It can never return
"nothing" or an implicit zero, so a missing classification, an unknown treatment or
missing configuration can never become "0 tax" or a confirmed result.

Engines:
  * NotConfiguredTaxEngine  - the default. Always raises (checkout answers 503).
  * FlatInterimTaxEngine    - the pre-GST interim behaviour (one configured percentage on the
                              merchandise subtotal). Its snapshot says so: gst_calculated=false.
  * GstTaxEngine            - the future GST engine. Today it only runs the readiness checks
                              (classification, approved rule on the date, known treatment) and
                              then refuses: "not implemented". It never returns a result.

This module only READS the tax master. It has no write path, so product and checkout
requests cannot modify tax classifications or rules.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from app.core.exceptions import InvalidStateError, ServiceUnavailableError
from app.modules.tax.repositories.tax_repository import TaxRepository

KNOWN_TREATMENTS = frozenset({"taxable", "exempt", "nil_rated", "non_gst"})

# Why tax cannot be determined for a line (stable strings, used in errors and tests).
MISSING_CLASSIFICATION = "missing_classification"
UNAPPROVED_CLASSIFICATION = "unapproved_classification"
NO_APPLICABLE_RULE = "no_applicable_rule"
AMBIGUOUS_RULE = "ambiguous_rule"
UNKNOWN_TREATMENT = "unknown_treatment"

# Configuration blockers (the business's own tax setup), not tied to a product.
NO_APPROVED_SETTINGS = "no_approved_tax_settings"
AMBIGUOUS_SETTINGS = "ambiguous_tax_settings"
REGISTRATION_NOT_APPROVED = "registration_not_approved"
REGISTRATION_NOT_VALID_ON_DATE = "registration_not_valid_on_date"

# Flip only when the GST calculator really exists (a later phase). While False, the readiness
# report can never show "ready" and the GST engine can never return a result.
GST_CALCULATOR_IMPLEMENTED = False


def india_today() -> date:
    """Today's date in India (UTC+05:30, no DST). Which date governs a supply is a professional decision."""
    return datetime.now(timezone(timedelta(hours=5, minutes=30))).date()


# ------------------------------------------------------------------ errors
class TaxNotConfiguredError(ServiceUnavailableError):
    code = "tax_not_configured"

    def __init__(self) -> None:
        super().__init__("Tax calculation is not configured")


class TaxEngineUnavailableError(ServiceUnavailableError):
    code = "tax_engine_not_implemented"

    def __init__(self, message: str = "GST calculation is not available yet") -> None:
        super().__init__(message)


class TaxCalculationBlockedError(InvalidStateError):
    """Tax cannot be determined for some items; nothing is assumed or defaulted."""

    code = "tax_calculation_blocked"

    def __init__(self, blockers: list["TaxBlocker"]) -> None:
        detail = "; ".join(
            f"product {b.product_id} ({b.reason})" if b.product_id is not None else f"configuration ({b.reason})"
            for b in blockers
        )
        super().__init__(f"Tax cannot be determined for: {detail}")
        self.blockers = blockers


# ------------------------------------------------------------------ contract
@dataclass(frozen=True)
class TaxLine:
    product_id: int
    quantity: int
    unit_price_paise: int
    classification_id: int | None


@dataclass(frozen=True)
class TaxRequest:
    lines: list[TaxLine]
    subtotal_paise: int
    shipping_paise: int
    as_of: date  # the date rules must be effective on (basis to be confirmed by the tax professional)


@dataclass(frozen=True)
class TaxBlocker:
    product_id: int | None  # None = the business configuration, not a product
    reason: str


@dataclass(frozen=True)
class TaxResult:
    """What an engine stands behind. Validated on construction; stored with the order."""

    tax_paise: int
    snapshot: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.tax_paise, int) or isinstance(self.tax_paise, bool) or self.tax_paise < 0:
            raise TaxEngineUnavailableError("The tax engine returned an invalid amount")
        if not self.snapshot.get("engine") or "gst_calculated" not in self.snapshot:
            raise TaxEngineUnavailableError("The tax engine returned no audit snapshot")


class TaxEngine(Protocol):
    is_configured: bool

    async def calculate(self, request: TaxRequest) -> TaxResult: ...


# ------------------------------------------------------------------ readiness (read-only)
async def evaluate_config(repository: TaxRepository, as_of: date):
    """The business's tax setup: exactly one approved configuration, under a valid approved registration.

    Returns (configuration or None, blockers). An empty blocker list means "configured", not "tax is 0".
    """
    settings = await repository.approved_settings_covering(as_of)
    if not settings:
        return None, [TaxBlocker(None, NO_APPROVED_SETTINGS)]
    if len(settings) > 1:
        return None, [TaxBlocker(None, AMBIGUOUS_SETTINGS)]
    config = settings[0]
    registration = await repository.get_registration(config.registration_id)
    if registration is None or registration.status != "approved":
        return config, [TaxBlocker(None, REGISTRATION_NOT_APPROVED)]
    if registration.valid_from > as_of or (registration.valid_to is not None and registration.valid_to <= as_of):
        return config, [TaxBlocker(None, REGISTRATION_NOT_VALID_ON_DATE)]
    return config, []



async def evaluate_readiness(
    repository: TaxRepository, lines: list[TaxLine], as_of: date
) -> list[TaxBlocker]:
    """Why tax cannot be determined for each line. An empty list means "ready", NOT "tax is 0"."""
    blockers: list[TaxBlocker] = []
    ids = {line.classification_id for line in lines if line.classification_id is not None}
    statuses = await repository.classification_statuses(ids)
    rules = await repository.approved_rules_covering(ids, as_of)
    for line in lines:
        cid = line.classification_id
        if cid is None:
            blockers.append(TaxBlocker(line.product_id, MISSING_CLASSIFICATION))
        elif statuses.get(cid) != "approved":
            blockers.append(TaxBlocker(line.product_id, UNAPPROVED_CLASSIFICATION))
        else:
            covering = rules.get(cid, [])
            if not covering:
                blockers.append(TaxBlocker(line.product_id, NO_APPLICABLE_RULE))
            elif len(covering) > 1:
                blockers.append(TaxBlocker(line.product_id, AMBIGUOUS_RULE))
            elif covering[0].treatment not in KNOWN_TREATMENTS:
                blockers.append(TaxBlocker(line.product_id, UNKNOWN_TREATMENT))
    return blockers


# ------------------------------------------------------------------ engines
class NotConfiguredTaxEngine:
    is_configured = False

    async def calculate(self, request: TaxRequest) -> TaxResult:
        raise TaxNotConfiguredError()


class FlatInterimTaxEngine:
    """The pre-GST interim behaviour: ONE configured percentage on the merchandise subtotal.

    Not GST: it ignores classifications and does not tax shipping. The snapshot says so
    explicitly so nobody mistakes it for a compliant calculation.
    """

    is_configured = True

    def __init__(self, rate_percent: Decimal | float | str) -> None:
        self.rate_percent = Decimal(str(rate_percent))

    async def calculate(self, request: TaxRequest) -> TaxResult:
        tax = int(
            (Decimal(request.subtotal_paise) * self.rate_percent / 100).quantize(
                Decimal(1), rounding=ROUND_HALF_UP
            )
        )
        return TaxResult(
            tax_paise=tax,
            snapshot={
                "version": 1,
                "engine": "flat_interim",
                "gst_calculated": False,
                "classification_used": False,
                "basis": "merchandise_subtotal",
                "rate_percent": str(self.rate_percent),
                "taxable_value_paise": request.subtotal_paise,
                "tax_paise": tax,
                "shipping_taxed": False,
                "rounding": "half_up_order_level",
                "lines": [
                    {"product_id": line.product_id, "tax_classification_id": line.classification_id}
                    for line in request.lines
                ],
            },
        )


class GstTaxEngine:
    """Placeholder for the GST calculator: readiness checks, then an explicit refusal."""

    is_configured = True

    def __init__(self, repository: TaxRepository) -> None:
        self.repository = repository

    async def calculate(self, request: TaxRequest) -> TaxResult:
        _config, blockers = await evaluate_config(self.repository, request.as_of)
        blockers = blockers + await evaluate_readiness(self.repository, request.lines, request.as_of)
        if blockers:
            raise TaxCalculationBlockedError(blockers)
        raise TaxEngineUnavailableError()
