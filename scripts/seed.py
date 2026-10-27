"""
Demo database seed: a small, believable Indian online store, for demos and development.

    docker compose exec api python -m scripts.seed

NEVER for production or staging (it creates accounts with publicly known passwords and payments that never happened). The
script refuses to run there.

It does not insert rows behind the application's back. It drives the application's OWN API in-process (the same code, rules and
database guards a real user and a real administrator go through), with a stand-in payment provider that never contacts anyone.
What you get, and which tables it fills:

    users, addresses ........ 2 admins (one approves tax records) + 6 customers, one saved address each
    store_settings .......... the store currency, chosen ONCE (INR)
    tax_classifications ..... approved HSN headings, assigned to the products
    categories, products .... 8 categories, 49 products with INR prices, descriptions and placeholder pictures
    carts, cart_items ....... three customers have something in their cart
    orders, order_items,
    order_status_history,
    payments, refunds ....... 16 orders over the last four weeks: fulfilled, paid, refunded, cancelled, cancelled-with-refund,
                              and one guest checkout
    audit_logs, background_jobs, refresh_tokens, processed_webhook_events: written by the flows above, as in real use

Deliberately left empty (they hold things only the client or a real provider can supply): tax_rules, gst_registrations,
business_tax_settings (rates and the client's legal identity), payment_provider_configs (real provider credentials),
payment_reconciliations, disputes, deferred_webhook_events (exceptions raised by real provider events),
order_currency_remediations (legacy data only).

Safe to run again: existing rows are left alone, the demo orders are only created once, and if this database already holds
real orders none are added. To start over: docker compose down -v   (this DELETES the development database).
"""

import asyncio
import json
import logging
import sys
from datetime import UTC, datetime

import httpx
from sqlalchemy import text

import app.jobs  # noqa: F401  (registers the background-job handlers)
from app.core.cache import close_cache, flush_pending
from app.core.config import HARDENED_ENVIRONMENTS, get_settings
from app.core.database import async_session_factory, engine
from app.core.jobs.worker import run_one
from app.core.rate_limit import limiter
from app.main import app
from app.modules.auth.repositories.auth_repository import AuthRepository
from app.modules.auth.services.auth_service import AuthService
from app.modules.payments.dependencies import payment_deps
from app.modules.payments.dependencies.payment_deps import get_payment_provider
from app.modules.users.repositories.user_repository import UserRepository
from app.shared.enums.roles import UserRole
from scripts import seed_data as data
from scripts.seed_demo_provider import DemoProvider
from scripts.seed_images import placeholder

API = get_settings().api_v1_prefix
settings = get_settings()


class SeedError(RuntimeError):
    pass


def check(response: httpx.Response, expected: tuple[int, ...], what: str) -> dict | list | None:
    if response.status_code not in expected:
        raise SeedError(f"{what} failed: HTTP {response.status_code} {response.text[:300]}")
    return response.json() if response.content else None


# ------------------------------------------------------------------------------------------------------------- setup
def apply_demo_checkout_settings() -> bool:
    """Checkout needs a shipping charge and a tax rate, and payments need their currency enabled; the application refuses to guess them. If .env has none, use the
    demo values FOR THIS RUN and tell the operator to put them in .env (the running api reads its own environment)."""
    used = False
    if not settings.payment_currencies_enabled.strip():
        settings.payment_currencies_enabled = "inr"
        used = True
    if settings.flat_shipping_cents is None:
        settings.flat_shipping_cents = data.DEMO_FLAT_SHIPPING_PAISE
        used = True
    if settings.tax_rate_percent is None:
        from decimal import Decimal

        settings.tax_rate_percent = Decimal(data.DEMO_TAX_RATE_PERCENT)
        used = True
    if not settings.free_shipping_threshold_cents:
        settings.free_shipping_threshold_cents = data.DEMO_FREE_SHIPPING_THRESHOLD_PAISE
        settings.free_shipping_threshold_basis = "subtotal"
        used = True
    return used


async def drain_jobs() -> None:
    """Runs queued background jobs (confirmation emails, refund execution) the way the worker would."""
    for _ in range(200):
        if not await run_one(async_session_factory):
            return


async def scalar(sql: str, **params):
    async with async_session_factory() as session:
        return (await session.execute(text(sql), params)).scalar_one()


async def rows(sql: str, **params):
    async with async_session_factory() as session:
        return (await session.execute(text(sql), params)).all()


# ------------------------------------------------------------------------------------------------------------- users
async def seed_users() -> None:
    async with async_session_factory() as db:
        auth = AuthService(AuthRepository(db))
        users = UserRepository(db)

        admin = await auth.repository.get_user_by_email(settings.seed_admin_email)
        if admin is None:
            admin = await auth.register(username="admin", email=settings.seed_admin_email, password=settings.seed_admin_password)
            await users.update(admin, role=UserRole.ADMIN, email_verified_at=datetime.now(UTC))
            print(f"  Created admin: {settings.seed_admin_email}")
        else:
            print(f"  Admin already exists: {settings.seed_admin_email}")

        approver = await auth.repository.get_user_by_email(data.APPROVER_EMAIL)
        if approver is None:
            approver = await auth.register(username=data.APPROVER_USERNAME, email=data.APPROVER_EMAIL, password=settings.seed_admin_password)
            await users.update(approver, role=UserRole.ADMIN, email_verified_at=datetime.now(UTC))
            print(f"  Created second admin (tax approver): {data.APPROVER_EMAIL}")
        else:
            print(f"  Second admin already exists: {data.APPROVER_EMAIL}")

        for c in data.CUSTOMERS:
            if await auth.repository.get_user_by_email(c.email) is None:
                customer = await auth.register(username=c.username, email=c.email, password=settings.seed_user_password)
                await users.update(customer, email_verified_at=datetime.now(UTC))  # demo accounts: no inbox exists to verify
                print(f"  Created customer: {c.email}")
            else:
                print(f"  Customer already exists: {c.email}")
        await db.commit()


async def login(client: httpx.AsyncClient, email: str, password: str) -> dict:
    r = await client.post(f"{API}/auth/login", json={"email": email, "password": password})
    token = check(r, (200,), f"login {email}")["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------------------------------------------------- catalogue
async def seed_store_currency(client, admin) -> None:
    r = await client.put(f"{API}/store/currency", json={"currency": "inr"}, headers=admin)
    if r.status_code == 409:
        current = (await client.get(f"{API}/store/currency")).json().get("currency")
        raise SeedError(
            f"This database's store currency is already '{current}' and a store currency can never be changed. The demo is "
            "priced in INR. Start from an empty development database: docker compose down -v, then up, migrate, seed."
        )
    check(r, (200,), "set store currency")
    print("  Store currency: INR")


async def seed_tax_classifications(client, admin, approver) -> dict[str, int]:
    existing = {}
    page = 1
    while True:
        batch = check(await client.get(f"{API}/admin/tax/classifications", params={"page": page, "page_size": 100}, headers=admin),
                      (200,), "list tax classifications")
        for item in batch["items"]:
            existing[item["code"]] = item
        if page * 100 >= batch["total"]:
            break
        page += 1
    ids: dict[str, int] = {}
    for code, description in data.HSN.items():
        item = existing.get(code)
        if item is None:
            item = check(await client.post(f"{API}/admin/tax/classifications", headers=admin, json={
                "kind": "hsn", "code": code, "description": description,
                "source_reference": "DEMO DATA: confirm this HSN heading and its rate with the client's tax advisor"}), (201,), f"create HSN {code}")
        if item["status"] == "draft":
            item = check(await client.post(f"{API}/admin/tax/classifications/{item['id']}/approve", headers=approver,
                                           json={"note": "Demo data approved by the seed"}), (200,), f"approve HSN {code}")
        ids[code] = item["id"]
    print(f"  Tax classifications (HSN): {len(ids)} approved")
    return ids


async def seed_categories(client, admin) -> dict[str, int]:
    found = {name: cid for cid, name in await rows("SELECT id, name FROM categories")}
    ids: dict[str, int] = {}
    for name, description, hue in data.CATEGORIES:
        if name in found:
            ids[name] = found[name]
            print(f"  Category already exists: {name}")
        else:
            ids[name] = check(await client.post(f"{API}/categories", json={"name": name, "description": description}, headers=admin),
                              (200, 201), f"create category {name}")["id"]
            print(f"  Created category: {name}")
        if await scalar("SELECT image_path FROM categories WHERE id = :i", i=ids[name]) is None:
            check(await client.post(f"{API}/categories/{ids[name]}/image", headers=admin,
                                    files={"image": (f"{ids[name]}.png", placeholder(hue), "image/png")}), (200,), f"category image {name}")
    return ids


async def seed_products(client, admin, category_ids: dict[str, int], hsn_ids: dict[str, int]) -> dict[str, int]:
    hue = {name: h for name, _d, h in data.CATEGORIES}
    existing = {sku: pid for pid, sku in await rows("SELECT id, sku FROM products")}
    created = updated = 0
    for i, p in enumerate(data.PRODUCTS):
        wanted = {
            "name": p.name, "description": p.description, "price_cents": p.paise, "category_id": category_ids[p.category],
            "tax_classification_id": hsn_ids[p.hsn] if p.hsn else None, "unit": "NOS",
        }
        if p.sku not in existing:
            r = await client.post(f"{API}/products", headers=admin, json={**wanted, "sku": p.sku, "stock": p.stock, "status": p.status})
            existing[p.sku] = check(r, (201,), f"create product {p.sku}")["id"]
            created += 1
        else:
            current = (await rows("SELECT name, description, price_cents, category_id, tax_classification_id, unit FROM products WHERE id = :i",
                                  i=existing[p.sku]))[0]
            if tuple(current) != tuple(wanted.values()):  # an earlier demo version: bring it up to date (stock is left alone)
                check(await client.patch(f"{API}/products/{existing[p.sku]}", headers=admin, json=wanted), (200,), f"update product {p.sku}")
                updated += 1
        if await scalar("SELECT image_path FROM products WHERE id = :i", i=existing[p.sku]) is None:
            check(await client.post(f"{API}/products/{existing[p.sku]}/image", headers=admin,
                                    files={"image": (f"{p.sku}.png", placeholder(hue[p.category], i), "image/png")}), (200,), f"image {p.sku}")
    print(f"  Products: {created} created, {updated} updated, {len(data.PRODUCTS) - created - updated} unchanged")
    return existing


# ------------------------------------------------------------------------------------------------------------- customers
async def seed_addresses_and_tokens(client) -> dict[int, dict]:
    sessions: dict[int, dict] = {}
    for idx, c in enumerate(data.CUSTOMERS):
        headers = await login(client, c.email, settings.seed_user_password)
        saved = check(await client.get(f"{API}/addresses", headers=headers), (200,), f"list addresses {c.email}")
        if not saved:
            saved = [check(await client.post(f"{API}/addresses", headers=headers, json={
                "recipient_name": c.recipient, "line1": c.line1, "line2": c.line2, "city": c.city, "state": c.state,
                "postal_code": c.postal_code, "country": "IN", "is_default": True}), (200, 201), f"address {c.email}")]
        sessions[idx] = {"headers": headers, "address_id": saved[0]["id"]}
    print(f"  Addresses: one saved address for each of {len(data.CUSTOMERS)} customers")
    return sessions


async def seed_carts(client, sessions, product_ids) -> None:
    filled = 0
    for idx, items in data.CARTS:
        h = sessions[idx]["headers"]
        if check(await client.get(f"{API}/cart", headers=h), (200,), "read cart")["items"]:
            continue
        for sku, qty in items:
            check(await client.post(f"{API}/cart/items", headers=h, json={"product_id": product_ids[sku], "quantity": qty}), (200, 201), f"cart {sku}")
        filled += 1
    print(f"  Carts: {filled} customers have items waiting in their cart")


# ------------------------------------------------------------------------------------------------------------- orders
class Webhooks:
    def __init__(self, client: httpx.AsyncClient, provider: DemoProvider) -> None:
        self.client, self.provider, self.n = client, provider, 0

    async def payment_succeeded(self) -> None:
        self.n += 1
        body = json.dumps({"event_id": f"evt_demo_{self.n}", "type": "payment_intent.succeeded", "pi": self.provider.last_payment_id})
        r = await self.client.post(f"{API}/payments/webhook", content=body, headers={"Stripe-Signature": "t=1,v1=demo"})
        check(r, (200,), "payment webhook")


async def place_orders(client, admin, sessions, product_ids, provider) -> dict[str, str]:
    hooks = Webhooks(client, provider)
    placed: dict[str, str] = {}
    for plan in data.ORDERS:
        guest_client = None
        if plan.customer is None:  # a guest: no account, a cart held by a cookie, paying with the order-scoped capability
            guest_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://seed.local")
            who, headers = guest_client, {}
        else:
            who, headers = client, sessions[plan.customer]["headers"]
        try:
            for sku, qty in plan.items:
                check(await who.post(f"{API}/cart/items", headers=headers, json={"product_id": product_ids[sku], "quantity": qty}),
                      (200, 201), f"{plan.key} add {sku}")
            if plan.customer is None:
                body = {"shipping_address": data.GUEST["address"], "guest_email": data.GUEST["email"]}
            else:
                body = {"address_id": sessions[plan.customer]["address_id"]}
            r = await who.post(f"{API}/orders/checkout", headers={**headers, "Idempotency-Key": plan.key}, json=body)
            order = check(r, (201,), f"{plan.key} checkout")
            oid = order["id"]
            placed[plan.key] = oid

            if plan.outcome == "customer_cancelled":
                check(await who.post(f"{API}/orders/{oid}/cancel", headers=headers), (200,), f"{plan.key} cancel")
                continue

            pay_headers = dict(headers)
            if plan.customer is None:
                cookie = next(c for c in r.headers.get_list("set-cookie") if c.startswith("guest_pay_"))
                pay_headers = {"Cookie": cookie.split(";", 1)[0]}
            check(await who.post(f"{API}/payments", headers=pay_headers, json={"order_id": oid}), (201,), f"{plan.key} pay")
            await hooks.payment_succeeded()

            if plan.outcome in ("fulfilled", "refunded"):
                check(await client.patch(f"{API}/orders/{oid}/status", headers=admin, json={"status": "fulfilled"}), (200,), f"{plan.key} fulfil")
            if plan.outcome == "refunded":
                check(await client.post(f"{API}/payments/{oid}/refund", headers=admin), (202,), f"{plan.key} refund")
            if plan.outcome == "cancel_and_refund":
                check(await client.post(f"{API}/admin/orders/{oid}/cancel-and-refund", headers=admin), (202,), f"{plan.key} cancel+refund")
            await drain_jobs()
        finally:
            if guest_client is not None:
                await guest_client.aclose()
        print(f"  Order {plan.key}: {plan.outcome}")
    return placed


async def backdate(placed: dict[str, str]) -> None:
    """Spreads the demo orders over the last four weeks (the API can only stamp 'now'). Shifts every timestamp on the order and
    the rows hanging off it by the same amount, so the history stays internally consistent. Skipped, with a notice, if the
    database refuses (it never changes amounts, currencies or statuses)."""
    plans = {p.key: p for p in data.ORDERS}
    try:
        async with async_session_factory() as session:
            for key, oid in placed.items():
                days = plans[key].days_ago
                for table, column in (("orders", "id"), ("order_status_history", "order_id"), ("payments", "order_id"), ("refunds", "order_id")):
                    cols = [r[0] for r in (await session.execute(text(
                        "SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name=:t "
                        "AND data_type LIKE 'timestamp%' AND column_name <> 'stock_released_at'"  # the database forbids touching it
                    ), {"t": table})).all()]
                    sets = ", ".join(f"{c} = {c} - (:d * interval '1 day')" for c in cols)
                    await session.execute(text(f"UPDATE {table} SET {sets} WHERE {column} = :o"), {"d": days, "o": oid})
                await session.execute(text(
                    "UPDATE audit_logs SET created_at = created_at - (:d * interval '1 day') WHERE target_id = :o"), {"d": days, "o": oid})
            await session.commit()
        print("  Order dates spread over the last four weeks")
    except Exception as exc:  # noqa: BLE001
        print(f"  (Order dates left as 'now': the database declined the date shift: {type(exc).__name__})")


async def seed_orders(client, admin, sessions, product_ids, provider) -> None:
    demo = await scalar("SELECT count(*) FROM orders WHERE idempotency_key LIKE 'demo-seed-%'")
    real = await scalar("SELECT count(*) FROM orders WHERE idempotency_key NOT LIKE 'demo-seed-%'")
    if real:
        print(f"  Orders: skipped, because this database already holds {real} order(s) that are not demo data")
        return
    if demo == len(data.ORDERS):
        print("  Orders: the demo orders already exist")
        return
    if demo:
        raise SeedError("A previous seed run stopped part-way. Start from an empty development database: docker compose down -v")
    placed = await place_orders(client, admin, sessions, product_ids, provider)
    await backdate(placed)
    await seed_carts(client, sessions, product_ids)


# ------------------------------------------------------------------------------------------------------------- main
async def summary() -> None:
    tables = ["users", "addresses", "store_settings", "tax_classifications", "categories", "products", "carts", "cart_items", "orders",
              "order_items", "order_status_history", "payments", "refunds", "audit_logs", "background_jobs", "refresh_tokens",
              "processed_webhook_events", "tax_rules", "gst_registrations", "business_tax_settings", "payment_provider_configs",
              "payment_reconciliations", "disputes", "deferred_webhook_events", "order_currency_remediations"]
    print("\nRows now in the database:")
    for t in tables:
        print(f"  {t:<28}{await scalar(f'SELECT count(*) FROM {t}')}")
    statuses = await rows("SELECT status::text, count(*) FROM orders GROUP BY 1 ORDER BY 1")
    print("\nOrders by status: " + ", ".join(f"{s}={n}" for s, n in statuses))


async def main() -> None:
    if settings.environment in HARDENED_ENVIRONMENTS:
        raise RuntimeError(
            f"Refusing to run the demo seed with ENVIRONMENT={settings.environment}. It creates accounts with publicly known "
            "passwords and payments that never happened. Never run it against a real deployment."
        )

    for noisy in ("app.access", "httpx", "app.core.jobs.worker", "app.modules.payments.jobs", "app.core.email.interface"):
        logging.getLogger(noisy).setLevel(logging.WARNING)  # the seed prints its own progress
    fallback = apply_demo_checkout_settings()
    limiter.enabled = False  # one process making many requests from one address is not abuse
    provider = DemoProvider()
    app.dependency_overrides[get_payment_provider] = lambda: provider
    payment_deps.get_payment_provider = lambda: provider  # what the background jobs use

    try:
        print("Seeding users...")
        await seed_users()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://seed.local", timeout=60) as client:
            admin = await login(client, settings.seed_admin_email, settings.seed_admin_password)
            approver = await login(client, data.APPROVER_EMAIL, settings.seed_admin_password)
            print("Store setup...")
            await seed_store_currency(client, admin)
            print("Tax classifications...")
            hsn_ids = await seed_tax_classifications(client, admin, approver)
            print("Categories...")
            category_ids = await seed_categories(client, admin)
            print("Products...")
            product_ids = await seed_products(client, admin, category_ids, hsn_ids)
            print("Customers...")
            sessions = await seed_addresses_and_tokens(client)
            print("Orders...")
            await seed_orders(client, admin, sessions, product_ids, provider)
        await flush_pending_cache()
        await summary()
    finally:
        app.dependency_overrides.pop(get_payment_provider, None)
        await close_cache()
        await engine.dispose()

    print("\nLog in at /docs (Authorize) or from your frontend:")
    print(f"  admin     {settings.seed_admin_email} / {settings.seed_admin_password}")
    print(f"  approver  {data.APPROVER_EMAIL} / {settings.seed_admin_password}   (second admin: approves tax records)")
    print(f"  customers {', '.join(c.email for c in data.CUSTOMERS)}  /  {settings.seed_user_password}")
    print("  These passwords are public: this data is for demos on your own machine only.")
    if fallback:
        print("\nIMPORTANT: your .env has no shipping/tax settings, so the seed used demo values for this run only. For the RUNNING")
        print("api (so a live checkout works in the demo), add these lines to .env and restart the api:\n")
        print(data.ENV_LINES)
        print("\n  (18% is an interim FLAT rate, not a GST calculation: tell the client so, and use their approved rate.)")


async def flush_pending_cache() -> None:
    async with async_session_factory() as db:
        await flush_pending(db)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SeedError as exc:
        print(f"\nSeed stopped: {exc}", file=sys.stderr)
        sys.exit(1)
