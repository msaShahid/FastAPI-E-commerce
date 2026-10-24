"""M1: deployment artifacts must not ship development behaviour. Static checks of the files in the repository root.

They skip when those files are not present (the development container mounts only app/, tests/, alembic/ and scripts/),
so they run in CI and on a checkout."""

import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent


def need(name: str) -> pathlib.Path:
    path = ROOT / name
    if not path.exists():
        pytest.skip(f"{name} is not part of this environment")
    return path


def test_the_default_image_is_a_production_image():
    text = need("Dockerfile").read_text()
    cmd = re.search(r"^CMD (.+)$", text, re.MULTILINE).group(1)
    assert "--reload" not in cmd and "--proxy-headers" in cmd
    assert re.search(r"^ARG INSTALL_DEV=false$", text, re.MULTILINE)  # production unless asked otherwise
    assert "COPY pyproject.toml uv.lock" in text and "--locked" in text  # exactly the locked versions, and a stale lockfile fails the build
    export = text[text.index("RUN uv export"): text.index("rm /tmp/requirements.txt")]
    assert '"$INSTALL_DEV" = "true"' in export and "--extra dev" in export and "--no-dev" not in export  # dev tools only behind the flag
    assert not re.search(r"^COPY .*tests", text, re.MULTILINE)  # tests are never baked in
    users = re.findall(r"^USER (\S+)$", text, re.MULTILINE)
    assert users and users[-1] not in ("root", "0")
    assert "HEALTHCHECK" in text


def test_production_compose_is_safe_and_complete():
    compose = yaml.safe_load(need("docker-compose.prod.yml").read_text())
    services = compose["services"]
    assert {"api", "worker", "db", "redis", "migrate"} <= set(services)
    for name in ("db", "redis"):
        assert "ports" not in services[name]  # never published
        assert "healthcheck" in services[name]
    assert services["api"]["ports"] == ["127.0.0.1:8000:8000"]  # only a local reverse proxy reaches it
    assert services["api"]["build"]["args"]["INSTALL_DEV"] == "false"
    for name in ("api", "worker"):
        assert services[name]["depends_on"]["db"]["condition"] == "service_healthy"
        assert services[name]["restart"] == "always"
        assert services[name]["env_file"] == [".env.production"]
        assert "no-new-privileges:true" in services[name]["security_opt"] and services[name]["cap_drop"] == ["ALL"]
    assert "reload" not in str(services["api"].get("command", ""))
    assert services["worker"]["healthcheck"] == {"disable": True}
    # migrations are a deliberate step, never run implicitly by `up`
    assert services["migrate"]["profiles"] == ["migrate"] and services["migrate"]["command"][-2:] == ["upgrade", "head"]
    assert "alembic" not in str(services["api"].get("command", ""))


def test_development_compose_keeps_reload_to_itself():
    """`--reload` is a development convenience and lives only in docker-compose.yml. How the development services' ports are
    published (all interfaces or this machine only) is the developer's choice and is deliberately not asserted here; the
    PRODUCTION file is checked for it above."""
    services = yaml.safe_load(need("docker-compose.yml").read_text())["services"]
    assert "--reload" in services["api"]["command"]


def test_the_production_env_template_fails_closed_until_it_is_filled_in():
    from dotenv import dotenv_values

    from app.core.config import ConfigurationError, Settings

    values = {k.lower(): v for k, v in dotenv_values(need(".env.production.example")).items() if v}
    assert values["environment"] == "production" and values["live_payments_enabled"] == "false"
    with pytest.raises(ConfigurationError):
        Settings(_env_file=None, **{k: v for k, v in values.items() if k in (
            "environment", "database_url", "jwt_secret", "stripe_secret_key", "stripe_webhook_secret")})


def test_dockerignore_keeps_secrets_and_clutter_out_of_the_build_context():
    lines = need(".dockerignore").read_text().split()
    assert ".env" in lines and ".env.*" in lines and "!.env.production.example" in lines


def test_sql_logging_is_off_by_default_and_not_tied_to_the_environment_label():
    from app.core.config import Settings
    from app.core.database import build_engine

    base = dict(database_url="postgresql+asyncpg://u:p@db/x", jwt_secret="x", stripe_secret_key="sk_test_x",
                stripe_webhook_secret="whsec_x")
    assert build_engine(Settings(_env_file=None, environment="development", **base)).echo is False
    assert build_engine(Settings(_env_file=None, environment="development", db_echo=True, **base)).echo is True
