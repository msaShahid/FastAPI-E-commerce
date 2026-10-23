"""Removing secrets and personal data from text that is logged or stored for operators.

`redact()` is deliberately a safety net applied at the edges (the log formatters, and the few places that persist an upstream
error message), not a licence to log sensitive values: call sites should still avoid them. It is idempotent, never raises, and
errs towards over-redaction. Everything here is a pattern, so it cannot find a secret that has no recognisable shape; that is
why the customer-facing paths do not rely on it (they return fixed messages instead)."""

import re

_RULES: list[tuple[re.Pattern[str], str]] = [
    # SQLAlchemy appends "[SQL: ...] [parameters: (...)]" to database errors: the parameters are customer data and hashes.
    (re.compile(r"\[parameters: .*?\](?=\s*(?:\(Background|$))", re.DOTALL | re.MULTILINE), "[parameters: REDACTED]"),
    # PostgreSQL "DETAIL: Key (email)=(someone@example.com) already exists." names the offending VALUE.
    (re.compile(r"(Key \([^)]*\)=)\(.*?\)(?= (?:already exists|is not present|is still referenced))", re.DOTALL), r"\1([REDACTED])"),
    # Provider credentials and per-payment secrets.
    (re.compile(r"\b(?:sk|rk)_(?:test|live)_[A-Za-z0-9_]+"), "[REDACTED_KEY]"),
    (re.compile(r"\bwhsec_[A-Za-z0-9_]+"), "[REDACTED_WEBHOOK_SECRET]"),
    (re.compile(r"\b(?:pi|seti)_[A-Za-z0-9]+_secret_[A-Za-z0-9_]+"), "[REDACTED_CLIENT_SECRET]"),
    (re.compile(r"\brzp_(?:test|live)_[A-Za-z0-9_]+"), "[REDACTED_KEY]"),
    # Tokens.
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"), "[REDACTED_JWT]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    # Whole header lines, and the cookies this application sets.
    (re.compile(r"(?i)\b(authorization|cookie|set-cookie|x-api-key)(['\"]?\s*[:=]\s*['\"]?)[^\r\n'\"]+"), r"\1\2[REDACTED]"),
    (re.compile(r"(?i)\b(guest_pay_[0-9a-f]{32}|guest_cart_token|refresh_token|access_token)(['\"]?\s*[=:]\s*['\"]?)[^;\s\"',}]+"),
     r"\1\2[REDACTED]"),
    # key=value / "key": "value" for anything named like a secret.
    (re.compile(r"(?i)\b(\w*(?:password|passwd|secret|api_key|private_key))(['\"]?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,}]+)"),
     r"\1\2[REDACTED]"),
    # Email addresses (personal data): keep the first character and the domain for debugging.
    (re.compile(r"\b([A-Za-z0-9])[A-Za-z0-9._%+-]*@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)"), r"\1***@\2"),
]


def redact(text: str) -> str:
    if not text:
        return text
    try:
        for pattern, replacement in _RULES:
            text = pattern.sub(replacement, text)
    except Exception:  # a log call must never fail because of this
        return "[unredactable text withheld]"
    return text


def mask_email(address: str) -> str:
    return redact(address) if "@" in address else "[no-address]"
