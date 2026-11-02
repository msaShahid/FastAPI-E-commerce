"""docs/openapi.json and docs/ERROR_CODES.md are generated from the code. They must match it, or the frontend would be built against a
contract that no longer exists. Regenerate with:  docker compose exec api python -m scripts.export_api_docs"""

from pathlib import Path

import pytest

from scripts.export_api_docs import DOCS, error_codes_text, openapi_text

HELP = "regenerate with: docker compose exec api python -m scripts.export_api_docs"


def _read(name: str) -> str:
    path = Path(DOCS) / name
    if not path.exists():  # a container image that does not ship docs/ (these files are checked wherever the repository is checked out)
        pytest.skip(f"{path} is not present in this environment")
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_the_published_openapi_file_matches_the_application():
    assert _read("openapi.json") == openapi_text(), f"docs/openapi.json is out of date: {HELP}"


def test_the_error_code_catalogue_matches_the_code():
    assert _read("ERROR_CODES.md") == error_codes_text(), f"docs/ERROR_CODES.md is out of date: {HELP}"
