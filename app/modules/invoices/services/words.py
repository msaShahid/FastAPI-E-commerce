"""An amount in words, Indian style ("Rupees Twelve Thousand Three Hundred Forty Five and Fifty Paise Only"), as printed on invoices."""

_ONES = ("", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen",
         "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen")
_TENS = ("", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety")


def _below_hundred(n: int) -> str:
    return _ONES[n] if n < 20 else (_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else ""))


def _below_thousand(n: int) -> str:
    parts = []
    if n >= 100:
        parts.append(_ONES[n // 100] + " Hundred")
        n %= 100
    if n:
        parts.append(_below_hundred(n))
    return " ".join(parts)


def number_in_words(n: int) -> str:
    """A whole number in the Indian system (thousand, lakh, crore)."""
    if n == 0:
        return "Zero"
    parts = []
    for name, size in (("Crore", 10_000_000), ("Lakh", 100_000), ("Thousand", 1_000)):
        if n >= size:
            parts.append(_below_hundred(n // size) if size > 1000 or n // size < 100 else _below_thousand(n // size))
            parts[-1] += " " + name
            n %= size
    if n:
        parts.append(_below_thousand(n))
    return " ".join(parts)


def amount_in_words(paise: int, currency: str = "INR") -> str:
    rupees, paisa = divmod(paise, 100)
    if currency.upper() != "INR":
        return ""
    text = f"Rupees {number_in_words(rupees)}"
    if paisa:
        text += f" and {number_in_words(paisa)} Paise"
    return text + " Only"
