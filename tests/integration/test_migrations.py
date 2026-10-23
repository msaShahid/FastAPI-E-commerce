"""M7: the migrations are exercised for real (the rest of the suite builds its schema with create_all and never runs one).

A scratch database is created next to the test database, driven with the real `alembic` command line, and dropped afterwards.
Nothing here touches the development or test databases' own data. Skipped if the database user cannot create databases."""

import os
import pathlib
import subprocess
import sys
import uuid

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
BEFORE_CURRENCY = "d6a9c4f1b873"  # the revision just before orders.currency / store_settings were introduced


def alembic(url, *args, expect_ok=True):
    env = {**os.environ, "DATABASE_URL": url}
    result = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=240)
    if expect_ok:
        assert result.returncode == 0, f"alembic {' '.join(args)} failed:\n{result.stdout[-1500:]}\n{result.stderr[-1500:]}"
    return result


@pytest_asyncio.fixture
async def scratch(test_engine):
    """(sqlalchemy url, asyncpg dsn) of a brand-new EMPTY database, created for one test and dropped afterwards."""
    base = make_url(str(test_engine.url.render_as_string(hide_password=False)))
    name = f"migration_scratch_{uuid.uuid4().hex[:10]}"
    admin_dsn = base.set(drivername="postgresql", database="postgres").render_as_string(hide_password=False)
    try:
        admin = await asyncpg.connect(admin_dsn)
        await admin.execute(f'CREATE DATABASE "{name}"')
    except Exception as exc:  # no CREATEDB privilege, or no server reachable that way
        pytest.skip(f"cannot create a scratch database: {type(exc).__name__}")
    url = base.set(database=name).render_as_string(hide_password=False)
    dsn = base.set(drivername="postgresql", database=name).render_as_string(hide_password=False)
    yield url, dsn
    await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    await admin.close()


async def fetch(dsn, sql, *args):
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetch(sql, *args)
    finally:
        await conn.close()


async def schema_fingerprint(run):
    """Everything alembic's own `check` ignores: triggers, check/exclusion constraints, indexes, function bodies, enum labels,
    and column definitions. `run(sql)` returns rows from the database being described."""
    queries = {
        "triggers": "SELECT tgrelid::regclass::text || '.' || tgname FROM pg_trigger WHERE NOT tgisinternal",
        # Foreign keys are compared by DEFINITION: the migrations name some explicitly while create_all uses the default name.
        "constraints": "SELECT conrelid::regclass::text || '.' || CASE WHEN contype = 'f' THEN '' ELSE conname END || ':' || "
                       "pg_get_constraintdef(oid) FROM pg_constraint WHERE connamespace = 'public'::regnamespace "
                       "AND contype IN ('c', 'u', 'x', 'f', 'p') AND conrelid::regclass::text <> 'alembic_version'",
        "indexes": "SELECT indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename <> 'alembic_version'",
        "functions": "SELECT proname || ':' || md5(prosrc) FROM pg_proc p JOIN pg_language l ON l.oid = p.prolang "
                     "WHERE p.pronamespace = 'public'::regnamespace AND l.lanname = 'plpgsql'",
        "enums": "SELECT t.typname || ':' || string_agg(e.enumlabel, ',' ORDER BY e.enumsortorder) FROM pg_type t "
                 "JOIN pg_enum e ON e.enumtypid = t.oid GROUP BY t.typname",
        "columns": "SELECT table_name || '.' || column_name || ':' || data_type || ':' || is_nullable || ':' || "
                   "coalesce(column_default, '') FROM information_schema.columns WHERE table_schema = 'public' "
                   "AND table_name <> 'alembic_version'",
    }
    return {name: sorted(str(r[0]) for r in await run(sql)) for name, sql in queries.items()}


@pytest.mark.asyncio
async def test_a_fresh_database_upgrades_to_one_head_with_no_drift(scratch):
    url, _ = scratch
    heads = alembic(url, "heads").stdout
    assert heads.count("(head)") == 1, heads  # two heads = two branches merged without `alembic merge`
    alembic(url, "upgrade", "head")
    alembic(url, "check")  # models and migrated schema agree


@pytest.mark.asyncio
async def test_the_migrated_schema_is_the_schema_the_test_suite_runs_against(scratch, db_session):
    """The suite builds its schema with create_all. This proves migrations produce the same triggers, constraints, indexes,
    function bodies, enums and columns, so the suite's results speak for what production actually has."""
    url, dsn = scratch
    alembic(url, "upgrade", "head")

    async def migrated(sql):
        return await fetch(dsn, sql)

    async def created(sql):
        return (await db_session.execute(text(sql))).all()

    assert await schema_fingerprint(migrated) == await schema_fingerprint(created)


@pytest.mark.asyncio
async def test_a_full_downgrade_leaves_nothing_behind_and_the_database_upgrades_again(scratch):
    url, dsn = scratch
    alembic(url, "upgrade", "head")
    alembic(url, "downgrade", "base")  # an empty database: the guard has nothing to protect
    tables = [r[0] for r in await fetch(dsn, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")]
    enums = await fetch(dsn, "SELECT typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
                             "WHERE n.nspname = 'public' AND t.typtype = 'e'")
    functions = await fetch(dsn, "SELECT proname FROM pg_proc p JOIN pg_language l ON l.oid = p.prolang "
                                 "WHERE p.pronamespace = 'public'::regnamespace AND l.lanname = 'plpgsql'")
    assert tables == ["alembic_version"] and enums == [] and functions == []
    alembic(url, "upgrade", "head")  # used to fail: `type "user_role" already exists`
    alembic(url, "check")


@pytest.mark.asyncio
async def test_a_downgrade_is_refused_on_a_database_with_data_and_changes_nothing(scratch):
    url, dsn = scratch
    alembic(url, "upgrade", "head")
    await fetch(dsn, "INSERT INTO store_settings (id, currency) VALUES (1, 'inr') ON CONFLICT DO NOTHING")
    await fetch(dsn, "INSERT INTO orders (id, guest_token, guest_email, idempotency_key, subtotal_cents, shipping_cents, "
                     "tax_cents, total_cents, status, currency) VALUES (gen_random_uuid(), 't', 'a@example.com', $1, 1000, 0, 0, "
                     "1000, 'pending', 'inr')", f"mig-{uuid.uuid4().hex[:8]}")
    head = alembic(url, "current").stdout
    refused = alembic(url, "downgrade", "-1", expect_ok=False)
    assert refused.returncode != 0 and "Refusing to downgrade" in refused.stderr + refused.stdout
    assert alembic(url, "current").stdout == head  # still at head: the whole attempt was rolled back
    assert (await fetch(dsn, "SELECT count(*) FROM orders"))[0][0] >= 1
    alembic(url, "-x", "allow_data_loss=true", "downgrade", "-1")  # an explicit, informed override (this step is reversible)
    assert alembic(url, "current").stdout != head
    alembic(url, "upgrade", "head")


@pytest.mark.asyncio
async def test_upgrading_an_older_database_with_legacy_rows_keeps_them_and_invents_no_currency(scratch):
    url, dsn = scratch
    alembic(url, "upgrade", BEFORE_CURRENCY)
    conn = await asyncpg.connect(dsn)
    try:
        user_id, order_id = uuid.uuid4(), uuid.uuid4()
        user_cols = await insert_row(conn, "users", {"id": user_id})
        await insert_row(conn, "orders", {"id": order_id, "user_id": user_id, "total_cents": 12345})
        await insert_row(conn, "payments", {"order_id": order_id, "amount_cents": 12345, "provider_payment_id": "pi_legacy"})
        assert user_cols  # (the helper filled every required column)
    finally:
        await conn.close()

    alembic(url, "upgrade", "head")
    alembic(url, "check")
    order = (await fetch(dsn, "SELECT total_cents, currency FROM orders WHERE id = $1", order_id))[0]
    payment = (await fetch(dsn, "SELECT amount_cents, currency FROM payments WHERE provider_payment_id = 'pi_legacy'"))[0]
    assert order["total_cents"] == 12345 and order["currency"] is None  # kept, and NEVER given a guessed currency
    assert payment["amount_cents"] == 12345  # untouched (its recorded currency is whatever the old default wrote)
    assert (await fetch(dsn, "SELECT count(*) FROM store_settings"))[0][0] == 0  # nothing invented for the store either


async def insert_row(conn, table, given):
    """Fills every NOT NULL column that has no default with a type-appropriate dummy value (legacy tables evolve; this keeps the
    test independent of their exact shape). `given` overrides specific columns."""
    columns = await conn.fetch(
        "SELECT column_name, data_type, udt_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = $1 "
        "AND is_nullable = 'NO' AND column_default IS NULL AND is_identity = 'NO'", table)
    values = dict(given)
    for col in columns:
        name = col["column_name"]
        if name in values:
            continue
        kind = col["data_type"]
        if kind == "uuid":
            values[name] = uuid.uuid4()
        elif kind == "USER-DEFINED":
            values[name] = (await conn.fetchval("SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                                                "WHERE t.typname = $1 ORDER BY e.enumsortorder LIMIT 1", col["udt_name"]))
        elif kind in ("integer", "bigint", "smallint"):
            values[name] = 1
        elif kind == "boolean":
            values[name] = False
        elif kind.startswith("timestamp"):
            import datetime
            values[name] = datetime.datetime.now()
        elif kind in ("json", "jsonb"):
            values[name] = "{}"
        else:
            values[name] = f"legacy-{name}"[:30]
    names = list(values)
    placeholders = ", ".join(f"${i + 1}" + ("::jsonb" if isinstance(values[n], str) and values[n] == "{}" and n in () else "") for i, n in enumerate(names))
    await conn.execute(f'INSERT INTO {table} ({", ".join(names)}) VALUES ({placeholders})', *[values[n] for n in names])
    return names
