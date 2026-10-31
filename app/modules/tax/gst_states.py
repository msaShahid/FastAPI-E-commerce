"""The GST state and union-territory codes, and a tolerant reader for the state a customer typed.

Place of supply for a sale to a consumer is the delivery state, so the delivery address's state must be turned into the two-digit GST
code (27 = Maharashtra). A state this module cannot recognise is never guessed: the caller refuses and asks the customer to choose.
The codes are the ones printed on GSTINs (the first two digits). 26 is the merged Dadra and Nagar Haveli and Daman and Diu; 97 is
"Other Territory"."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class GstState:
    code: str
    name: str


STATES: tuple[GstState, ...] = tuple(GstState(c, n) for c, n in (
    ("01", "Jammu and Kashmir"), ("02", "Himachal Pradesh"), ("03", "Punjab"), ("04", "Chandigarh"), ("05", "Uttarakhand"),
    ("06", "Haryana"), ("07", "Delhi"), ("08", "Rajasthan"), ("09", "Uttar Pradesh"), ("10", "Bihar"), ("11", "Sikkim"),
    ("12", "Arunachal Pradesh"), ("13", "Nagaland"), ("14", "Manipur"), ("15", "Mizoram"), ("16", "Tripura"), ("17", "Meghalaya"),
    ("18", "Assam"), ("19", "West Bengal"), ("20", "Jharkhand"), ("21", "Odisha"), ("22", "Chhattisgarh"), ("23", "Madhya Pradesh"),
    ("24", "Gujarat"), ("26", "Dadra and Nagar Haveli and Daman and Diu"), ("27", "Maharashtra"), ("29", "Karnataka"), ("30", "Goa"),
    ("31", "Lakshadweep"), ("32", "Kerala"), ("33", "Tamil Nadu"), ("34", "Puducherry"), ("35", "Andaman and Nicobar Islands"),
    ("36", "Telangana"), ("37", "Andhra Pradesh"), ("38", "Ladakh"), ("97", "Other Territory"),
))
BY_CODE = {s.code: s for s in STATES}

# Common spellings and the two-letter postal abbreviations (ISO 3166-2:IN) that address forms often use.
_ALIASES = {
    "orissa": "21", "uttaranchal": "05", "pondicherry": "34", "new delhi": "07", "nct of delhi": "07", "nct delhi": "07",
    "delhi ncr": "07", "j and k": "01", "jammu kashmir": "01", "chattisgarh": "22", "telengana": "36", "tamilnadu": "33",
    "andaman nicobar": "35", "andaman nicobar islands": "35", "andaman and nicobar": "35", "dadra and nagar haveli": "26",
    "daman and diu": "26", "dadra nagar haveli": "26", "dnhdd": "26", "dnh and dd": "26", "andaman and nicobar islands": "35",
    "dadra and nagar haveli and daman and diu": "26", "other territory": "97",
}
_ABBREVIATIONS = {
    "an": "35", "ap": "37", "ar": "12", "as": "18", "br": "10", "ch": "04", "ct": "22", "cg": "22", "dn": "26", "dd": "26", "dh": "26",
    "dl": "07", "ga": "30", "gj": "24", "hr": "06", "hp": "02", "jk": "01", "jh": "20", "ka": "29", "kl": "32", "la": "38", "ld": "31",
    "mp": "23", "mh": "27", "mn": "14", "ml": "17", "mz": "15", "nl": "13", "or": "21", "od": "21", "py": "34", "pb": "03", "rj": "08",
    "sk": "11", "tn": "33", "ts": "36", "tg": "36", "tr": "16", "up": "09", "uk": "05", "ut": "05", "wb": "19",
}
_BY_NORMALISED = {}


def _normalise(text: str) -> str:
    text = text.casefold().replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"^(state of|union territory of|ut of)\s+", "", re.sub(r"\s+", " ", text).strip())
    return re.sub(r"\s+", " ", text).strip()


for _s in STATES:
    _BY_NORMALISED[_normalise(_s.name)] = _s.code
_BY_NORMALISED.update(_ALIASES)
_BY_NORMALISED.update(_ABBREVIATIONS)


def resolve_state(text: str | None) -> GstState | None:
    """The GST state for what a customer typed (a name, a common spelling, a postal abbreviation or the numeric code), else None."""
    if not text:
        return None
    key = _normalise(text)
    if key in BY_CODE:
        return BY_CODE[key]
    code = _BY_NORMALISED.get(key)
    return BY_CODE[code] if code else None
