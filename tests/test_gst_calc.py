"""The GST arithmetic, against hand-worked numbers and against invariants that must hold for ANY input (no database)."""

import random
from decimal import Decimal as D

import pytest

from app.modules.tax.gst_calc import CalcLine, calculate


def line(value, cgst="9", sgst="9", igst="18", treatment="taxable", key=1):
    return CalcLine(key, value, treatment, D(cgst), D(sgst), D(igst))


def run(lines, *, shipping=0, intra=True, mode="exclusive", ship="not_taxed", rounding="line_half_up"):
    return calculate(lines, shipping_paise=shipping, intra_state=intra, pricing_mode=mode, shipping_treatment=ship, rounding_policy=rounding)


def amounts(r):
    return (r.taxable_paise, r.cgst_paise, r.sgst_paise, r.igst_paise, r.round_off_paise, r.tax_added_paise, r.tax_included_paise)


# ---------------------------------------------------------------- worked examples
def test_exclusive_intra_state_adds_cgst_and_sgst():
    r = run([line(100000)])                                   # Rs 1,000.00 at 9% + 9%
    assert amounts(r) == (100000, 9000, 9000, 0, 0, 18000, 0) and r.tax_paise == 18000


def test_exclusive_inter_state_adds_igst_only():
    r = run([line(49900, igst="5", cgst="2.5", sgst="2.5")], intra=False)
    assert amounts(r) == (49900, 0, 0, 2495, 0, 2495, 0)


def test_inclusive_prices_contain_the_tax_and_add_nothing_to_what_the_customer_pays():
    r = run([line(118000)], mode="inclusive")
    assert amounts(r) == (100000, 9000, 9000, 0, 0, 0, 18000)      # Rs 1,180.00 = 1,000 + 90 + 90


def test_inclusive_rounding_gives_equal_cgst_and_sgst_and_an_explicit_round_off():
    r = run([line(99900)], mode="inclusive")                    # 99900 / 1.18 = 84661.0169...
    assert r.lines[0].taxable_paise == 84661 and r.cgst_paise == r.sgst_paise == 7619 and r.round_off_paise == 1
    assert r.taxable_paise + r.tax_paise + r.round_off_paise == 99900


def test_inclusive_inter_state_uses_igst():
    r = run([line(105000, igst="5", cgst="2.5", sgst="2.5")], mode="inclusive", intra=False)
    assert amounts(r) == (100000, 0, 0, 5000, 0, 0, 5000)


def test_each_component_is_rounded_to_the_paisa_half_up():
    r = run([line(1001, cgst="2.5", sgst="2.5")])               # 25.025 -> 25 each, not 50.05 in one go
    assert (r.cgst_paise, r.sgst_paise, r.tax_paise) == (25, 25, 50)
    assert run([line(1010, cgst="2.5", sgst="2.5")]).cgst_paise == 25       # 25.25 -> 25
    assert run([line(1020, cgst="2.5", sgst="2.5")]).cgst_paise == 26       # 25.5 -> 26: half rounds up


def test_line_rounding_and_invoice_rounding_can_differ_by_a_paisa_and_both_add_up():
    lines = [line(1010, cgst="2.5", sgst="2.5", key=k) for k in (1, 2, 3)]
    per_line = run(lines, rounding="line_half_up")
    per_invoice = run(lines, rounding="invoice_half_up")
    assert per_line.cgst_paise == 75 and per_invoice.cgst_paise == 76         # 3 x 25.25 -> 75, but 75.75 on the whole slab -> 76
    assert sum(x.cgst_paise for x in per_invoice.lines) == 76


def test_taxable_shipping_is_spread_over_the_lines_in_proportion_and_taxed_at_their_rates():
    r = run([line(60000, key=1), line(40000, key=2)], shipping=5000, ship="taxable_with_goods")
    assert [x.shipping_paise for x in r.lines] == [3000, 2000]
    assert [x.cgst_paise for x in r.lines] == [5670, 3780] and r.taxable_paise == 105000 and r.shipping_untaxed_paise == 0


def test_untaxed_shipping_carries_no_tax_and_is_reported_separately():
    r = run([line(60000, key=1), line(40000, key=2)], shipping=5000, ship="not_taxed")
    assert r.cgst_paise == 9000 and r.taxable_paise == 100000 and r.shipping_untaxed_paise == 5000


def test_spreading_shipping_never_loses_a_paisa():
    r = run([line(33333, key=1), line(66667, key=2)], shipping=1000, ship="taxable_with_goods")
    assert [x.shipping_paise for x in r.lines] == [333, 667] and sum(x.shipping_paise for x in r.lines) == 1000


def test_mixed_rates_are_taxed_each_at_its_own_rate():
    r = run([line(100000, "9", "9", "18", key=1), line(100000, "2.5", "2.5", "5", key=2)])
    assert (r.lines[0].tax_paise, r.lines[1].tax_paise, r.tax_paise) == (18000, 5000, 23000)


@pytest.mark.parametrize("treatment", ["exempt", "nil_rated", "non_gst"])
def test_exempt_nil_rated_and_non_gst_lines_carry_no_tax_in_either_pricing_mode(treatment):
    for mode in ("exclusive", "inclusive"):
        r = run([line(12345, treatment=treatment)], mode=mode)
        assert r.taxable_paise == 12345 and r.tax_paise == 0 and r.round_off_paise == 0


def test_a_zero_rated_taxable_line_has_no_tax():
    assert run([line(5000, "0", "0", "0")]).tax_paise == 0


def test_unknown_configuration_values_are_refused_not_defaulted():
    for bad in ({"mode": "gross"}, {"ship": "free"}, {"rounding": "banker"}):
        with pytest.raises(ValueError):
            run([line(100)], **bad)


# ---------------------------------------------------------------- invariants for any input
def random_case(rng):
    n = rng.randint(1, 6)
    slabs = [("0", "0", "0"), ("1.5", "1.5", "3"), ("2.5", "2.5", "5"), ("6", "6", "12"), ("9", "9", "18"), ("14", "14", "28")]
    lines = []
    for k in range(n):
        c, s, g = rng.choice(slabs)
        lines.append(line(rng.randint(1, 500000), c, s, g, treatment=rng.choice(["taxable"] * 5 + ["exempt", "nil_rated", "non_gst"]), key=k))
    return lines, rng.choice([0, 0, rng.randint(1, 20000)])


@pytest.mark.parametrize("seed", range(300))
def test_invariants_hold_for_random_baskets_in_every_configuration(seed):
    rng = random.Random(seed)
    lines, shipping = random_case(rng)
    for mode in ("exclusive", "inclusive"):
        for ship in ("not_taxed", "taxable_with_goods"):
            for rounding in ("line_half_up", "invoice_half_up"):
                for intra in (True, False):
                    r = run(lines, shipping=shipping, intra=intra, mode=mode, ship=ship, rounding=rounding)
                    assert len(r.lines) == len(lines) and all(min(x.taxable_paise, x.cgst_paise, x.sgst_paise, x.igst_paise) >= 0 for x in r.lines)
                    # the lines add up to the invoice totals
                    assert r.taxable_paise == sum(x.taxable_paise for x in r.lines)
                    assert r.tax_paise == r.cgst_paise + r.sgst_paise + r.igst_paise == sum(x.tax_paise for x in r.lines)
                    assert r.round_off_paise == sum(x.round_off_paise for x in r.lines)
                    # a sale is either within the state (CGST + SGST) or between states (IGST), never both
                    assert (r.igst_paise == 0) if intra else (r.cgst_paise == 0 and r.sgst_paise == 0)
                    # shipping is spread exactly, or reported as untaxed, never lost
                    taxed_shipping = sum(x.shipping_paise for x in r.lines)
                    assert taxed_shipping + r.shipping_untaxed_paise == shipping
                    assert sum(x.gross_paise for x in r.lines) == sum(x.value_paise for x in lines) + taxed_shipping
                    if mode == "inclusive":
                        # the customer pays exactly the listed gross: nothing is added, and the books balance with the round off
                        assert r.tax_added_paise == 0 and r.tax_included_paise == r.tax_paise
                        assert sum(x.gross_paise for x in r.lines) == r.taxable_paise + r.tax_paise + r.round_off_paise
                        assert all(abs(x.round_off_paise) <= 2 for x in r.lines) or rounding == "invoice_half_up"
                    else:
                        assert r.tax_added_paise == r.tax_paise and r.tax_included_paise == 0 and r.round_off_paise == 0
                        assert r.taxable_paise == sum(x.gross_paise for x in r.lines)
                    if intra and all(x.cgst == x.sgst for x in lines) and rounding == "line_half_up" and mode == "exclusive":
                        assert r.cgst_paise == r.sgst_paise              # equal rates give equal CGST and SGST
