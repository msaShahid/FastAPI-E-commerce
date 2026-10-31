"""Reading the state a customer typed into the GST state code (place of supply). Never guessed: unknown means unknown."""

import pytest

from app.modules.tax.gst_states import BY_CODE, STATES, resolve_state


def test_the_table_has_unique_codes_and_names_and_the_codes_printed_on_gstins():
    assert len({s.code for s in STATES}) == len(STATES) == len({s.name for s in STATES})
    assert BY_CODE["27"].name == "Maharashtra" and BY_CODE["07"].name == "Delhi" and BY_CODE["29"].name == "Karnataka"
    assert BY_CODE["36"].name == "Telangana" and BY_CODE["38"].name == "Ladakh" and "28" not in BY_CODE and "25" not in BY_CODE


@pytest.mark.parametrize("text", ["Maharashtra", "maharashtra", "  MAHARASHTRA  ", "MH", "mh", "27", "Maharashtra.", "State of Maharashtra"])
def test_the_same_state_is_recognised_however_it_is_written(text):
    assert resolve_state(text).code == "27"


@pytest.mark.parametrize(("text", "code"), [
    ("Orissa", "21"), ("Odisha", "21"), ("OD", "21"), ("Uttaranchal", "05"), ("UK", "05"), ("Pondicherry", "34"), ("New Delhi", "07"),
    ("NCT of Delhi", "07"), ("DL", "07"), ("Jammu & Kashmir", "01"), ("Jammu and Kashmir", "01"), ("J&K", "01"), ("Tamilnadu", "33"),
    ("TN", "33"), ("Telangana", "36"), ("TS", "36"), ("Chattisgarh", "22"), ("Andaman & Nicobar Islands", "35"),
    ("Dadra and Nagar Haveli", "26"), ("Daman & Diu", "26"), ("Daman and Diu", "26"), ("West Bengal", "19"), ("WB", "19"), ("Ladakh", "38"),
])
def test_old_names_common_misspellings_and_postal_abbreviations_are_recognised(text, code):
    assert resolve_state(text).code == code


@pytest.mark.parametrize("text", [None, "", "   ", "Atlantis", "Mahrashtra", "India", "99", "00", "Texas", "M"])
def test_anything_unrecognised_is_not_guessed(text):
    assert resolve_state(text) is None


def test_every_listed_state_is_recognised_by_its_own_name_and_code():
    for state in STATES:
        assert resolve_state(state.name) == state and resolve_state(state.code) == state
