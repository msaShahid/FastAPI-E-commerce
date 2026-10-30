"""Writes the two machine-generated files a frontend needs, from the code itself (never by hand):

  docs/openapi.json    every endpoint, request and response shape (generate typed clients from this)
  docs/ERROR_CODES.md  every stable error `code` the API can return, with its HTTP status

    docker compose exec api python -m scripts.export_api_docs

A test (tests/test_api_docs_current.py) fails when either file is out of date, so they cannot silently drift from the API."""

import importlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

STATUS_BY_BASE = [  # first match wins
    ("UpstreamServiceError", 502), ("ServiceUnavailableError", 503), ("InvalidStateError", 422), ("NotFoundError", 404),
    ("ConflictError", 409), ("ForbiddenError", 403), ("UnauthorizedError", 401), ("BadRequestError", 400),
]


def _all_subclasses(cls):
    for sub in cls.__subclasses__():
        yield sub
        yield from _all_subclasses(sub)


def openapi_text() -> str:
    from app.main import app

    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def error_codes_text() -> str:
    import app.main  # noqa: F401  (imports every module that defines errors)

    for file in sorted((ROOT / "app").rglob("*.py")):  # also modules the routers do not import directly
        parts = file.relative_to(ROOT).with_suffix("").parts
        if "exceptions" in parts[-1] or "exceptions" in parts[-2:-1] or parts[-1].endswith("_exceptions"):
            importlib.import_module(".".join(parts))
    from app.core.exceptions import AppError

    rows: dict[str, tuple[int, str, str, str]] = {}
    for cls in sorted(set(_all_subclasses(AppError)), key=lambda c: (c.__module__, c.__name__)):
        code = cls.__dict__.get("code")
        if not isinstance(code, str):
            continue
        status = next((s for base, s in STATUS_BY_BASE if any(b.__name__ == base for b in cls.__mro__)), 500)
        try:
            meaning = str(cls())
        except TypeError:
            meaning = (cls.__doc__ or "").strip().splitlines()[0] if cls.__doc__ else ""
        meaning = re.sub(r"\s+", " ", meaning).strip()
        rows[code] = (status, meaning, cls.__name__, cls.__module__.removeprefix("app.modules.").removeprefix("app."))
    lines = [
        "# Error codes", "",
        "Generated from the code by `python -m scripts.export_api_docs` (do not edit by hand). Every error body is "
        "`{\"detail\": ..., \"request_id\": ...}`; where the error has a stable code it also has `\"code\"`. Branch on `code`, never on the "
        "wording of `detail`. Errors without a code (a missing resource, an invalid token, not enough stock) are identified by their HTTP status.", "",
        "| HTTP | code | meaning (default wording) | defined in |", "|---|---|---|---|",
    ]
    for code, (status, meaning, name, module) in sorted(rows.items(), key=lambda kv: (kv[1][0], kv[0])):
        lines.append(f"| {status} | `{code}` | {meaning.replace('|', '/')} | `{module}.{name}` |")
    lines += ["", f"{len(rows)} codes.", ""]
    return "\n".join(lines)


def main() -> int:
    sys.path.insert(0, str(ROOT))
    DOCS.mkdir(exist_ok=True)
    (DOCS / "openapi.json").write_text(openapi_text(), encoding="utf-8", newline="\n")
    (DOCS / "ERROR_CODES.md").write_text(error_codes_text(), encoding="utf-8", newline="\n")
    print("wrote docs/openapi.json and docs/ERROR_CODES.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
