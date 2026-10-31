"""The GST arithmetic. Pure functions on integers (paise) and Decimal: no database, no clock, no configuration lookup.

Every choice that is a professional decision arrives as an argument (it comes from the business's APPROVED tax configuration):
  pricing_mode      'exclusive': the listed price is before GST, tax is added.   'inclusive': the listed price already contains GST.
  shipping          'not_taxed': the shipping charge carries no GST.   'taxable_with_goods': it is taxed like the goods it ships with
                    (spread over the lines in proportion to their value, each part at its line's rate; see _allocate).
  rounding_policy   'line_half_up': tax is rounded to the paisa for each line.
                    'invoice_half_up': tax is rounded once per rate slab across the whole invoice and then spread back to the lines.
Rates come from approved rules. Intra-state sales use CGST + SGST/UTGST, inter-state sales use IGST.

Every result satisfies, exactly and in whole paise (the tests assert it):
  exclusive:  the customer pays taxable + tax (the tax is added), round_off is 0.
  inclusive:  the customer pays the listed gross value; taxable and each tax component are rounded to the paisa on their own, and the
              (at most a paisa or so) difference is the invoice's "round off": gross == taxable + tax + round_off.
  CGST and SGST are each computed from the taxable value, so they are always equal when their rates are equal.
  The lines add up to the invoice totals: nothing is lost or invented by rounding."""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

ZERO = Decimal(0)
HUNDRED = Decimal(100)


def half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class CalcLine:
    key: int                  # the caller's id (the product id); only used to hand results back
    value_paise: int          # unit price x quantity, exactly as the customer sees it
    treatment: str            # taxable | exempt | nil_rated | non_gst
    cgst: Decimal             # percent
    sgst: Decimal             # percent (SGST or UTGST)
    igst: Decimal             # percent


@dataclass(frozen=True)
class LineTax:
    key: int
    shipping_paise: int       # the part of the shipping charge spread onto this line (0 when shipping is not taxed)
    gross_paise: int          # value_paise + shipping_paise: what the customer pays for this line
    taxable_paise: int
    cgst_paise: int
    sgst_paise: int
    igst_paise: int
    round_off_paise: int      # inclusive pricing only: gross - taxable - tax (otherwise 0)

    @property
    def tax_paise(self) -> int:
        return self.cgst_paise + self.sgst_paise + self.igst_paise


@dataclass(frozen=True)
class GstResult:
    lines: list[LineTax]
    shipping_untaxed_paise: int
    taxable_paise: int
    cgst_paise: int
    sgst_paise: int
    igst_paise: int
    round_off_paise: int
    tax_paise: int            # all the GST on the invoice
    tax_added_paise: int      # the part ADDED to what the customer pays (all of it when exclusive, none when inclusive)
    tax_included_paise: int   # the part already inside the listed prices


def _allocate(total: int, weights: list[int]) -> list[int]:
    """Splits `total` whole paise over `weights` in proportion, exactly (largest remainder): the parts always add up to `total`."""
    weight_sum = sum(weights)
    if total == 0 or weight_sum == 0:
        return [0] * len(weights)
    exact = [Decimal(total) * w / weight_sum for w in weights]
    parts = [int(e) for e in exact]  # floor (all non-negative)
    leftover = total - sum(parts)
    order = sorted(range(len(weights)), key=lambda i: (-(exact[i] - parts[i]), i))
    for i in order[:leftover]:
        parts[i] += 1
    return parts


def _rates(line: CalcLine, intra_state: bool) -> tuple[Decimal, Decimal, Decimal]:
    if line.treatment != "taxable":
        return ZERO, ZERO, ZERO
    return (line.cgst, line.sgst, ZERO) if intra_state else (ZERO, ZERO, line.igst)


def _slab_tax(value: int, rates: tuple[Decimal, Decimal, Decimal], pricing_mode: str) -> tuple[int, int, int, int]:
    """(taxable, cgst, sgst, igst) for ONE value at ONE set of rates, each rounded half-up to the paisa."""
    total_rate = sum(rates)
    if total_rate == 0:
        return value, 0, 0, 0
    if pricing_mode == "exclusive":
        return value, half_up(Decimal(value) * rates[0] / HUNDRED), half_up(Decimal(value) * rates[1] / HUNDRED), half_up(Decimal(value) * rates[2] / HUNDRED)
    taxable = half_up(Decimal(value) * HUNDRED / (HUNDRED + total_rate))  # inclusive: the value contains the tax
    return taxable, half_up(Decimal(taxable) * rates[0] / HUNDRED), half_up(Decimal(taxable) * rates[1] / HUNDRED), half_up(Decimal(taxable) * rates[2] / HUNDRED)


def calculate(
    lines: list[CalcLine], *, shipping_paise: int, intra_state: bool, pricing_mode: str, shipping_treatment: str, rounding_policy: str,
) -> GstResult:
    if pricing_mode not in ("inclusive", "exclusive") or shipping_treatment not in ("not_taxed", "taxable_with_goods") \
            or rounding_policy not in ("line_half_up", "invoice_half_up"):
        raise ValueError("unknown tax configuration value")
    values = [line.value_paise for line in lines]
    if shipping_treatment == "taxable_with_goods" and shipping_paise:
        shipped = _allocate(shipping_paise, values)
        untaxed_shipping = 0
    else:
        shipped = [0] * len(lines)
        untaxed_shipping = shipping_paise
    grosses = [v + s for v, s in zip(values, shipped, strict=True)]
    rates = [_rates(line, intra_state) for line in lines]

    taxable = [0] * len(lines)
    cgst = [0] * len(lines)
    sgst = [0] * len(lines)
    igst = [0] * len(lines)
    if rounding_policy == "line_half_up":
        for i in range(len(lines)):
            taxable[i], cgst[i], sgst[i], igst[i] = _slab_tax(grosses[i], rates[i], pricing_mode)
    else:
        slabs: dict[tuple[Decimal, Decimal, Decimal], list[int]] = {}
        for i, r in enumerate(rates):
            slabs.setdefault(r, []).append(i)
        for r, members in slabs.items():
            weights = [grosses[i] for i in members]
            t, c, s, g = _slab_tax(sum(weights), r, pricing_mode)
            for amount, target in ((t, taxable), (c, cgst), (s, sgst), (g, igst)):
                for i, part in zip(members, _allocate(amount, weights), strict=True):
                    target[i] = part
    per_line = [
        LineTax(lines[i].key, shipped[i], grosses[i], taxable[i], cgst[i], sgst[i], igst[i],
                grosses[i] - taxable[i] - cgst[i] - sgst[i] - igst[i] if pricing_mode == "inclusive" else 0)
        for i in range(len(lines))
    ]
    totals = [sum(x) for x in (taxable, cgst, sgst, igst)]
    tax_total = totals[1] + totals[2] + totals[3]
    return GstResult(
        lines=per_line, shipping_untaxed_paise=untaxed_shipping, taxable_paise=totals[0], cgst_paise=totals[1], sgst_paise=totals[2],
        igst_paise=totals[3], round_off_paise=sum(line.round_off_paise for line in per_line), tax_paise=tax_total,
        tax_added_paise=tax_total if pricing_mode == "exclusive" else 0,
        tax_included_paise=tax_total if pricing_mode == "inclusive" else 0,
    )
