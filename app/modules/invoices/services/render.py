"""The printable invoice page. Rendered from the stored (frozen) document with autoescaping on: nothing in it is ever interpreted as HTML."""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

_ENV = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parent.parent / "templates")), autoescape=select_autoescape(["html"]))


def inr(paise: int | None) -> str:
    """12345678 paise -> 1,23,456.78 (Indian digit grouping)."""
    if paise is None:
        return ""
    sign = "-" if paise < 0 else ""
    rupees, paisa = divmod(abs(int(paise)), 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join([*groups, tail])
    return f"{sign}{digits}.{paisa:02d}"


_ENV.filters["inr"] = inr


def render_invoice_html(document: dict) -> str:
    return _ENV.get_template("invoice.html").render(d=document)
