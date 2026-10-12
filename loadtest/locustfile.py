"""
Load test for the e-commerce API (Locust).

Two kinds of virtual user hit the API at the same time:

  BrowserUser  Read-only shoppers: product list / search / sort, product
               detail, categories. This is the bulk of real traffic.

  ShopperUser  Guest shoppers who all fight over ONE low-stock product:
               add 1 unit to the cart, then check out. This is the
               scenario that matters most -- it proves checkout never
               sells more units than exist, even under heavy concurrency.

Guest checkout is used on purpose: /auth/register (5/min) and /auth/login
(10/min) are rate limited per IP, and a load test comes from one IP, so
creating a user per virtual user would just measure the rate limiter.

At the end of the run the script reads the product back and checks:

    stock left == initial stock - orders that were created      (no lost/phantom stock)
    stock left >= 0                                              (never oversold)

It sets a non-zero exit code if either is violated, if any request failed
unexpectedly (5xx, bad status), or if the overall p95 latency is above
LOADTEST_MAX_P95_MS -- so it can gate a deploy or a CI job.

This WRITES DATA (a category, a product, guest carts and orders). Run it
against a dev/staging stack, never production. See loadtest/README.md.
"""

import logging
import os
import random
import uuid
from urllib.parse import urlparse

from locust import HttpUser, between, events, task

API = "/api/v1"

ADMIN_EMAIL = os.getenv("LOADTEST_ADMIN_EMAIL", "")
ADMIN_PASSWORD = os.getenv("LOADTEST_ADMIN_PASSWORD", "")
STOCK = int(os.getenv("LOADTEST_STOCK", "10"))
MAX_P95_MS = int(os.getenv("LOADTEST_MAX_P95_MS", "2000"))

# Hosts that are obviously a local/dev stack. Anything else needs an
# explicit opt-in, because this test creates real rows.
SAFE_HOSTS = {"localhost", "127.0.0.1", "api", "host.docker.internal"}

log = logging.getLogger("loadtest")

# Shared state for the whole run. Locust runs users as gevent greenlets in
# one thread, so plain ints are safe to increment.
state = {
    "product_id": None,
    "initial_stock": STOCK,
    "orders_created": 0,
    "sold_out": 0,
    "replays_checked": 0,
    "browse_product_ids": [],
}


def _admin_login(client) -> dict:
    response = client.post(
        f"{API}/auth/login",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
        name="setup: login",
    )
    response.raise_for_status()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@events.test_start.add_listener
def on_test_start(environment, **_kwargs):
    host = environment.host or ""
    hostname = urlparse(host).hostname
    if hostname not in SAFE_HOSTS and os.getenv("LOADTEST_ALLOW_REMOTE") != "1":
        _abort(environment, f"Refusing to write test data to {host!r}. "
                            "Set LOADTEST_ALLOW_REMOTE=1 only for a non-production target.")
        return
    if not ADMIN_EMAIL or not ADMIN_PASSWORD:
        _abort(environment, "Set LOADTEST_ADMIN_EMAIL and LOADTEST_ADMIN_PASSWORD "
                            "(an admin account is needed to create the limited-stock product).")
        return

    import requests

    session = requests.Session()
    base = host.rstrip("/")

    class _Client:  # minimal adapter so _admin_login works outside a Locust user
        def post(self, path, json=None, name=None, headers=None):
            return session.post(base + path, json=json, headers=headers, timeout=30)

    try:
        headers = _admin_login(_Client())
        run_id = uuid.uuid4().hex[:8]
        category = session.post(
            f"{base}{API}/categories",
            json={"name": f"Load test {run_id}", "description": "Created by loadtest"},
            headers=headers, timeout=30,
        )
        category.raise_for_status()
        product = session.post(
            f"{base}{API}/products",
            json={
                "name": f"Load test limited item {run_id}",
                "description": "Created by loadtest",
                "price_cents": 1999,
                "sku": f"LT-{run_id}",
                "stock": STOCK,
                "category_id": category.json()["id"],
                "status": "active",
            },
            headers=headers, timeout=30,
        )
        product.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - any setup failure aborts the run
        _abort(environment, f"Setup failed: {exc}")
        return

    state["product_id"] = product.json()["id"]
    state["admin_headers"] = headers
    log.info("Created limited-stock product id=%s with stock=%s", state["product_id"], STOCK)


def _abort(environment, message: str) -> None:
    log.error(message)
    environment.process_exit_code = 1
    if environment.runner:
        environment.runner.quit()


@events.test_stop.add_listener
def on_test_stop(environment, **_kwargs):
    if state["product_id"] is None:
        return

    import requests

    base = (environment.host or "").rstrip("/")
    problems = []

    response = requests.get(f"{base}{API}/products/{state['product_id']}",
                            headers=state.get("admin_headers"), timeout=30)
    if response.status_code != 200:
        problems.append(f"could not read the product back (HTTP {response.status_code})")
        remaining = None
    else:
        remaining = response.json()["stock"]
        expected = state["initial_stock"] - state["orders_created"]
        if remaining < 0:
            problems.append(f"OVERSOLD: stock is {remaining}")
        if remaining != expected:
            problems.append(
                f"stock mismatch: {state['orders_created']} orders created from "
                f"{state['initial_stock']} units, so {expected} should be left, found {remaining}"
            )

    p95 = environment.stats.total.get_response_time_percentile(0.95) or 0
    if p95 > MAX_P95_MS:
        problems.append(f"p95 latency {p95:.0f} ms is above the {MAX_P95_MS} ms limit")

    if environment.stats.total.num_failures:
        problems.append(f"{environment.stats.total.num_failures} request(s) failed unexpectedly")

    # Retire the product so it stops showing up in the shop.
    requests.patch(f"{base}{API}/products/{state['product_id']}", json={"status": "archived"},
                   headers=state.get("admin_headers"), timeout=30)

    print("\n" + "=" * 64)
    print("CHECKOUT INVARIANT")
    print(f"  initial stock          : {state['initial_stock']}")
    print(f"  orders created (201)   : {state['orders_created']}")
    print(f"  rejected as sold out   : {state['sold_out']}")
    print(f"  idempotent replays ok  : {state['replays_checked']}")
    print(f"  stock left (from API)  : {remaining}")
    print(f"  p95 latency            : {p95:.0f} ms (limit {MAX_P95_MS} ms)")
    print("RESULT: " + ("PASS" if not problems else "FAIL"))
    for problem in problems:
        print(f"  - {problem}")
    print("=" * 64)

    if problems:
        environment.process_exit_code = 1


class BrowserUser(HttpUser):
    """Anonymous visitor reading the catalogue."""

    weight = 7
    wait_time = between(0.5, 2)

    def on_start(self):
        # Learn some real product ids once, so detail pages hit real rows.
        if not state["browse_product_ids"]:
            response = self.client.get(f"{API}/products", params={"page_size": 50},
                                       name="setup: list products")
            if response.ok:
                state["browse_product_ids"] = [p["id"] for p in response.json()["items"]]

    @task(5)
    def list_products(self):
        self.client.get(f"{API}/products", params={"page": random.randint(1, 3)},
                        name=f"{API}/products")

    @task(2)
    def search_and_sort(self):
        params = {
            "search": random.choice(["a", "e", "load", "test"]),
            "sort": random.choice(["price_cents", "-price_cents", "name", "-created_at"]),
        }
        self.client.get(f"{API}/products", params=params, name=f"{API}/products?search&sort")

    @task(3)
    def product_detail(self):
        ids = state["browse_product_ids"]
        if not ids:
            return
        self.client.get(f"{API}/products/{random.choice(ids)}", name=f"{API}/products/[id]")

    @task(1)
    def list_categories(self):
        self.client.get(f"{API}/categories", name=f"{API}/categories")


class ShopperUser(HttpUser):
    """Guest shopper racing for the limited-stock product."""

    weight = 3
    wait_time = between(0.05, 0.3)  # short on purpose: maximise contention

    @task
    def buy_limited_item(self):
        product_id = state["product_id"]
        if product_id is None:
            return

        # 1. Add one unit to this visitor's guest cart (the API sets a cookie).
        with self.client.post(f"{API}/cart/items",
                              json={"product_id": product_id, "quantity": 1},
                              name=f"{API}/cart/items", catch_response=True) as response:
            if response.status_code == 201:
                response.success()
            elif response.status_code == 422:  # not enough stock left
                response.success()
                state["sold_out"] += 1
                return
            else:
                response.failure(f"add to cart: HTTP {response.status_code} {response.text[:120]}")
                return

        # 2. Check out. The Idempotency-Key makes a retried request safe.
        key = str(uuid.uuid4())
        body = {
            "guest_email": f"shopper-{uuid.uuid4().hex[:10]}@loadtest.example",
            "shipping_address": {
                "recipient_name": "Load Test",
                "line1": "1 Test Street",
                "city": "Testville",
                "state": "CA",
                "postal_code": "94000",
                "country": "US",
            },
        }
        with self.client.post(f"{API}/orders/checkout", json=body,
                              headers={"Idempotency-Key": key},
                              name=f"{API}/orders/checkout", catch_response=True) as response:
            if response.status_code == 201:
                response.success()
                state["orders_created"] += 1
                order_id = response.json()["id"]
            elif response.status_code == 422:  # someone else bought the last unit first
                response.success()
                state["sold_out"] += 1
                self.client.delete(f"{API}/cart", name=f"{API}/cart (clear)")
                return
            else:
                response.failure(f"checkout: HTTP {response.status_code} {response.text[:120]}")
                self.client.delete(f"{API}/cart", name=f"{API}/cart (clear)")
                return

        # 3. Occasionally replay the identical request, as a flaky client
        #    would. It must return the SAME order and must not take stock again
        #    (the end-of-run stock check would catch it if it did).
        if random.random() < 0.2:
            with self.client.post(f"{API}/orders/checkout", json=body,
                                  headers={"Idempotency-Key": key},
                                  name=f"{API}/orders/checkout (replay)",
                                  catch_response=True) as replay:
                if replay.status_code in (200, 201) and replay.json().get("id") == order_id:
                    replay.success()
                    state["replays_checked"] += 1
                else:
                    replay.failure(f"idempotent replay: HTTP {replay.status_code} {replay.text[:120]}")
