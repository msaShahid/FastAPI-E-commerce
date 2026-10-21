"""
READ-ONLY report of orders/carts whose ownership breaks the invariant (exactly one of: registered user, guest).

    docker compose exec api python -m scripts.check_ownership_violations

Exit code 0 = none, 1 = violations found. It changes nothing. Used before migration d4b7e1a9c635, which refuses to run
while violations exist (it never deletes data or invents an owner).
"""

import asyncio
import sys

from sqlalchemy import text

from app.core.database import engine

CHECKS = {
    "orders": "(user_id IS NOT NULL AND guest_token IS NULL AND guest_email IS NULL) OR "
              "(user_id IS NULL AND guest_token IS NOT NULL AND guest_email IS NOT NULL)",
    "carts": "(user_id IS NOT NULL AND guest_token IS NULL) OR (user_id IS NULL AND guest_token IS NOT NULL)",
}


async def main() -> int:
    found = 0
    async with engine.connect() as conn:
        for table, check in CHECKS.items():
            rows = (await conn.execute(text(
                f"SELECT id, user_id IS NOT NULL AS has_user, guest_token IS NOT NULL AS has_token FROM {table} "  # noqa: S608
                f"WHERE NOT ({check}) ORDER BY created_at"))).all()
            print(f"{table}: {len(rows)} violating row(s)")
            for row in rows:
                print(f"  id={row[0]} has_user={row[1]} has_guest_token={row[2]}")
            found += len(rows)
    await engine.dispose()
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
