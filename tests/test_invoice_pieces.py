"""The small pure parts of invoicing: amount in words, financial years, number format, Indian digit grouping."""

from datetime import date

import pytest

from app.modules.invoices.services.invoice_service import fiscal_year, format_number
from app.modules.invoices.services.render import inr
from app.modules.invoices.services.words import amount_in_words, number_in_words


@pytest.mark.parametrize(("n", "words"), [
    (0, "Zero"), (7, "Seven"), (19, "Nineteen"), (20, "Twenty"), (45, "Forty Five"), (100, "One Hundred"), (101, "One Hundred One"),
    (999, "Nine Hundred Ninety Nine"), (1000, "One Thousand"), (1234, "One Thousand Two Hundred Thirty Four"),
    (99999, "Ninety Nine Thousand Nine Hundred Ninety Nine"), (100000, "One Lakh"), (123456, "One Lakh Twenty Three Thousand Four Hundred Fifty Six"),
    (10000000, "One Crore"), (123456789, "Twelve Crore Thirty Four Lakh Fifty Six Thousand Seven Hundred Eighty Nine"),
])
def test_numbers_are_written_in_the_indian_system(n, words):
    assert number_in_words(n) == words


def test_amounts_in_words_name_rupees_and_paise():
    assert amount_in_words(123000) == "Rupees One Thousand Two Hundred Thirty Only"
    assert amount_in_words(123050) == "Rupees One Thousand Two Hundred Thirty and Fifty Paise Only"
    assert amount_in_words(5) == "Rupees Zero and Five Paise Only"
    assert amount_in_words(100, "usd") == ""                                        # only rupees are written out


@pytest.mark.parametrize(("day", "fy"), [(date(2026, 4, 1), "2026-27"), (date(2027, 3, 31), "2026-27"), (date(2026, 3, 31), "2025-26"),
                                         (date(2026, 1, 15), "2025-26"), (date(2026, 12, 31), "2026-27"), (date(2099, 4, 1), "2099-00")])
def test_the_financial_year_runs_from_april_to_march(day, fy):
    assert fiscal_year(day) == fy


def test_invoice_numbers_fit_the_16_character_limit_even_at_the_longest_prefix():
    assert format_number("INV", "2026-27", 1) == "INV2627/000001"
    assert format_number("ABCDE", "2026-27", 999999) == "ABCDE2627/999999" and len(format_number("ABCDE", "2026-27", 999999)) == 16
    assert format_number("CN", "2025-26", 42) == "CN2526/000042"


@pytest.mark.parametrize(("paise", "text"), [(0, "0.00"), (5, "0.05"), (99900, "999.00"), (123456, "1,234.56"), (12345678, "1,23,456.78"),
                                             (1234567890, "1,23,45,678.90"), (-250, "-2.50"), (None, "")])
def test_amounts_use_indian_digit_grouping(paise, text):
    assert inr(paise) == text
