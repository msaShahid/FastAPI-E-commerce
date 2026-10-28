"""Validation of what an administrator types about a shipment. Pure functions (no database), each returning the cleaned value or
raising ValueError with a message safe to show. The tracking URL becomes a link on the customer's order page, so it is checked as
if it were hostile: https only, a real public-looking host, no embedded credentials, no whitespace or control characters."""

import ipaddress
import re
from urllib.parse import urlsplit

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_CARRIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .&'()/-]{1,59}$")
_TRACKING = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{2,63}$")
_HOST_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
MAX_URL_LENGTH = 2048


def carrier_name(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip()
    if _CONTROL.search(value) or not _CARRIER.fullmatch(cleaned):
        raise ValueError("Carrier name must be 2-60 characters: letters, digits, spaces and . & ' ( ) / -")
    return cleaned


def tracking_number(value: str) -> str:
    cleaned = value.strip()
    if not _TRACKING.fullmatch(cleaned):
        raise ValueError("Tracking number must be 3-64 characters: letters, digits and . _ / - (no spaces)")
    return cleaned


def tracking_url(value: str) -> str:
    url = value.strip()
    if not url or len(url) > MAX_URL_LENGTH:
        raise ValueError(f"Tracking URL must be 1-{MAX_URL_LENGTH} characters")
    if _CONTROL.search(url) or re.search(r"\s", url):
        raise ValueError("Tracking URL must not contain spaces or control characters (encode them, e.g. %20)")
    if not url.isascii():
        raise ValueError("Tracking URL must be plain ASCII (percent-encode any other characters)")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ValueError("Tracking URL is not a valid web address") from exc
    if parts.scheme.lower() != "https":
        raise ValueError("Tracking URL must start with https://")
    if parts.username is not None or parts.password is not None or "@" in (parts.netloc or ""):
        raise ValueError("Tracking URL must not contain a username or password")
    host = (parts.hostname or "").lower()
    if not host or port not in (None, 443):
        raise ValueError("Tracking URL must have a normal website address (no custom port)")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass  # not an IP literal: good
    else:
        raise ValueError("Tracking URL must use the carrier's website name, not an IP address")
    labels = host.split(".")
    if len(labels) < 2 or host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        raise ValueError("Tracking URL must be on a public website")
    if not all(_HOST_LABEL.fullmatch(label) for label in labels) or not re.fullmatch(r"[a-z]{2,}", labels[-1]):
        raise ValueError("Tracking URL has an invalid website name")
    return url
