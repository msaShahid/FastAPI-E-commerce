"""
Fails if a NAMED constraint declared in the models is missing from the migrated database.

`alembic check` does not compare CHECK or EXCLUDE constraints, so a model can declare a
guarantee that no migration creates (tests build their schema from the models and would
not notice). Run after `alembic upgrade head`:

    python -m scripts.check_schema_constraints [--allow name1,name2]

--allow lists KNOWN gaps being tracked elsewhere; they are reported but do not fail.
"""

import argparse
import asyncio
import sys

import asyncpg
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import ExcludeConstraint

import app.main  # noqa: F401  (imports every model so the metadata is complete)
from app.core.config import get_settings
from app.core.database import Base


def declared() -> dict[str, str]:
    """constraint name -> table, for every named check / unique / exclude constraint."""
    out: dict[str, str] = {}
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            kinds = (CheckConstraint, UniqueConstraint, ExcludeConstraint)
            if isinstance(constraint, kinds) and constraint.name:
                out[str(constraint.name)] = table.name
    return out


async def existing() -> set[str]:
    url = get_settings().database_url.replace("+asyncpg", "")
    conn = await asyncpg.connect(url)
    try:
        rows = await conn.fetch("SELECT conname FROM pg_constraint WHERE contype IN ('c', 'u', 'x')")
        return {row["conname"] for row in rows}
    finally:
        await conn.close()


async def main(allow: set[str]) -> int:
    want = declared()
    have = await existing()
    missing = sorted(name for name in want if name not in have)
    known = [n for n in missing if n in allow]
    new = [n for n in missing if n not in allow]
    for name in known:
        print(f"KNOWN GAP (allowed): {name} on {want[name]}")
    for name in new:
        print(f"MISSING: {name} on {want[name]}")
    print(f"checked {len(want)} named constraints: {len(new)} missing, {len(known)} known gaps")
    return 1 if new else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow", default="", help="comma-separated names of tracked gaps")
    args = parser.parse_args()
    sys.exit(asyncio.run(main({n for n in args.allow.split(",") if n})))
