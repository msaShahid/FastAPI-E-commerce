"""The example environment files must stay true to the code: every name is a real setting (or a documented extra), and the production example
ships the safe choices. They skip when the files are not part of this environment (the development container mounts only app/ and tests/)."""

import pathlib
import re

import pytest

from app.core.config import Settings

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXTRAS = {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "FORWARDED_ALLOW_IPS"}  # read by Postgres and uvicorn, not by Settings
FIELDS = {name.upper() for name in Settings.model_fields}


def read(name: str) -> dict[str, str]:
    path = ROOT / name
    if not path.exists():
        pytest.skip(f"{name} is not part of this environment")
    values = {}
    for line in path.read_text().splitlines():
        match = re.match(r"^#?\s*([A-Z][A-Z0-9_]+)=(.*?)(\s+#.*)?$", line)
        if match and not line.startswith("# Every") and "=" in line:
            values[match.group(1)] = match.group(2).strip()
    return values


@pytest.mark.parametrize("name", [".env.example", ".env.production.example"])
def test_every_name_is_a_real_setting(name):
    unknown = sorted(set(read(name)) - FIELDS - EXTRAS)
    assert not unknown, f"{name} names settings that do not exist: {unknown}"


def test_the_production_example_ships_the_safe_choices():
    env = read(".env.production.example")
    assert env["ENVIRONMENT"] == "production" and env["GRAPHQL_ENABLED"] == "false" and env["LOG_FORMAT"] == "json"
    assert env["FRONTEND_REVALIDATE_URL"].startswith("http://") and "FORWARDED_ALLOW_IPS" in env and env["REFRESH_REUSE_GRACE_SECONDS"] == "10"
    assert not any(v.startswith("sk_live_") for v in env.values())  # no real-looking live key can be committed by accident


async def test_graphql_can_be_switched_off(client, monkeypatch):
    from app.core.config import get_settings

    query = {"query": "{ __typename }"}
    assert (await client.post("/graphql", json=query)).status_code != 404  # on by default (it answers 401 without a login)
    monkeypatch.setattr(get_settings(), "graphql_enabled", False)
    assert (await client.post("/graphql", json=query)).status_code == 404
