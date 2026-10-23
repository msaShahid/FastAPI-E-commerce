"""TESTS ONLY. The application can never change a store's currency once it is set, so a test that needs a store initialised
in a particular currency re-creates the (order-free) store_settings row directly, as a database-level reset would."""

from sqlalchemy import text

FACTORY = None  # set by the test_session_factory fixture in tests/conftest.py


async def reset_store_currency(code: str) -> None:
    async with FACTORY() as session:
        current = (await session.execute(text("SELECT currency FROM store_settings WHERE id = 1"))).scalar_one_or_none()
        if current == code:
            return
        await session.execute(text("DELETE FROM store_settings"))  # refused by the database once any order exists
        await session.execute(text("INSERT INTO store_settings (id, currency) VALUES (1, :c)"), {"c": code})
        await session.commit()
