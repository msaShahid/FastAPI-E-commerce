"""
Tax-engine integration boundary.

Checkout asks a TaxEngine for the tax on a cart. The contract is deliberately strict:
an engine either returns a TaxResult it stands behind, or raises. It can never return
"nothing" or an implicit zero, so a missing classification, an unknown treatment or
missing configuration can never become "0 tax" or a confirmed result.

Engines:
  * NotConfiguredTaxEngine  - the default. Always raises (checkout answers 503).
  * FlatInterimTaxEngine    - the pre-GST interim behaviour (one configured percentage on the
                              merchandise subtotal). Its snapshot says so: gst_calculated=false.
  * GstTaxEngine            - the GST engine. It runs the readiness checks (classification, approved rule on
                              the date, known treatment, approved business configuration and registration),
                              works out the place of supply from the delivery state, and then does the
                              arithmetic in gst_calc.py using ONLY approved master data. Anything it cannot
                              determine it refuses (never a guess, never an implicit zero).

This module only READS the tax master. It has no write path, so product and checkout
requests cannot modify tax classifications or rules.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from app.core.exceptions import InvalidStateError, ServiceUnavailableError
from app.modules.tax.gst_calc import CalcLine, calculate
from app.modules.tax.gst_states import resolve_state
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

# The GST calculator exists (gst_calc.py). It is only USED when TAX_CALCULATION_ENABLED is on, and then only with an approved
# business configuration, registration, classifications and rules: without them every checkout is refused, never taxed at zero.
GST_CALCULATOR_IMPLEMENTED = True


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


class DeliveryStateUnknownError(InvalidStateError):
    """Place of supply is the delivery state; if it cannot be recognised nothing is assumed."""

    code = "delivery_state_unknown"

    def __init__(self, state: str | None = None) -> None:
        shown = f" '{state}'" if state else ""
        super().__init__(f"We could not recognise the delivery state{shown}. Please choose the state from the list.")


class DeliveryCountryNotSupportedError(InvalidStateError):
    code = "delivery_country_not_supported"

    def __init__(self) -> None:
        super().__init__("Delivery outside India is not supported")


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
    discount_paise: int = 0   # this line's share of a coupon discount: tax is on the price AFTER it


@dataclass(frozen=True)
class TaxRequest:
    lines: list[TaxLine]
    subtotal_paise: int
    shipping_paise: int
    as_of: date  # the date rules must be effective on (basis to be confirmed by the tax professional)
    delivery_state: str | None = None    # as the customer wrote it; the GST engine turns it into a state code (place of supply)
    delivery_country: str | None = None  # ISO 3166-1 alpha-2


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
    """Goods and Services Tax, calculated from approved master data only. See the module docstring and gst_calc.py."""

    is_configured = True

    def __init__(self, repository: TaxRepository) -> None:
        self.repository = repository

    async def calculate(self, request: TaxRequest) -> TaxResult:
        config, blockers = await evaluate_config(self.repository, request.as_of)
        blockers = blockers + await evaluate_readiness(self.repository, request.lines, request.as_of)
        if blockers:
            raise TaxCalculationBlockedError(blockers)
        registration = await self.repository.get_registration(config.registration_id)

        if (request.delivery_country or "").upper() != "IN":
            raise DeliveryCountryNotSupportedError()
        place = resolve_state(request.delivery_state)
        if place is None:
            raise DeliveryStateUnknownError(request.delivery_state)
        intra_state = place.code == registration.state_code

        ids = {line.classification_id for line in request.lines}
        rules = await self.repository.approved_rules_covering(ids, request.as_of)      # exactly one each: readiness checked
        classifications = await self.repository.classifications_by_ids(ids)
        calc_lines = []
        for index, line in enumerate(request.lines):
            rule = rules[line.classification_id][0]
            calc_lines.append(CalcLine(index, line.unit_price_paise * line.quantity - line.discount_paise, rule.treatment, rule.cgst_rate, rule.sgst_utgst_rate, rule.igst_rate))
        result = calculate(
            calc_lines, shipping_paise=request.shipping_paise, intra_state=intra_state, pricing_mode=config.pricing_mode,
            shipping_treatment=config.shipping_tax_treatment, rounding_policy=config.rounding_policy,
        )
        lines_out = []
        for line, calc, tax in zip(request.lines, calc_lines, result.lines, strict=True):
            rule = rules[line.classification_id][0]
            classification = classifications[line.classification_id]
            lines_out.append({
                "product_id": line.product_id, "classification_id": line.classification_id, "hsn_kind": classification.kind,
                "hsn_code": classification.code, "treatment": rule.treatment, "quantity": line.quantity,
                "unit_price_paise": line.unit_price_paise, "discount_paise": line.discount_paise, "value_paise": calc.value_paise,
                "shipping_paise": tax.shipping_paise,
                "gross_paise": tax.gross_paise, "taxable_paise": tax.taxable_paise, "cgst_paise": tax.cgst_paise,
                "sgst_paise": tax.sgst_paise, "igst_paise": tax.igst_paise, "tax_paise": tax.tax_paise,
                "round_off_paise": tax.round_off_paise,
                "rates": {"cgst": str(rule.cgst_rate), "sgst": str(rule.sgst_utgst_rate), "igst": str(rule.igst_rate)},
            })
        return TaxResult(
            tax_paise=result.tax_added_paise,
            snapshot={
                "version": 2, "engine": "gst", "gst_calculated": True, "classification_used": True,
                "as_of": request.as_of.isoformat(), "pricing_mode": config.pricing_mode, "rounding_policy": config.rounding_policy,
                "shipping_tax_treatment": config.shipping_tax_treatment,
                "seller": {"registration_id": registration.id, "gstin": registration.gstin, "legal_name": registration.legal_name,
                           "state_code": registration.state_code},
                "place_of_supply": {"state_code": place.code, "state_name": place.name, "basis": config.place_of_supply_basis},
                "supply_type": "intra_state" if intra_state else "inter_state",
                "lines": lines_out,
                "shipping": {"paise": request.shipping_paise, "taxed": bool(request.shipping_paise) and config.shipping_tax_treatment == "taxable_with_goods",
                             "untaxed_paise": result.shipping_untaxed_paise},
                "totals": {
                    "taxable_paise": result.taxable_paise, "cgst_paise": result.cgst_paise, "sgst_paise": result.sgst_paise,
                    "igst_paise": result.igst_paise, "tax_paise": result.tax_paise, "tax_added_paise": result.tax_added_paise,
                    "tax_included_paise": result.tax_included_paise, "round_off_paise": result.round_off_paise,
                },
                "tax_paise": result.tax_added_paise,
            },
        )
