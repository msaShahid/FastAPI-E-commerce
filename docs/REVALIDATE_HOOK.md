# Cache invalidation hook (backend dependency BE-C2)

The storefront caches backend reads by tag with a safety-net TTL: **product pages 30 s, listings and categories 60 s, store settings 60 s, site config 60 s (`CONFIG_TTL_SECONDS`)**.
Without the hook, a change can take up to that long to appear. With the hook, it appears on the **next request**: tags are expired immediately (not stale-while-revalidate).

The backend calls the hook **after a change is committed**. The frontend owns the endpoint; the backend owns the calls.

## Endpoint

`POST http://web:3000/api/bff/revalidate` on the **private network only**. Caddy does not route this path to the internet (tested in `tests/e2e/proxy.spec.ts`).

| Header           | Value                                                                          |
| ---------------- | ------------------------------------------------------------------------------ |
| `Content-Type`   | `application/json`                                                             |
| `x-nc-timestamp` | Unix time in seconds (must be within 5 minutes of the web server's clock)      |
| `x-nc-signature` | Lower-case hex `HMAC-SHA256(REVALIDATE_HMAC_SECRET, "<timestamp>.<raw body>")` |

Body: `{"tags": ["catalogue", "product:12"]}` (1 to 50 tags, each matching `site-config | catalogue | store-settings | product:<id> | category:<id>`).
Responses: `200 {"ok":true,"revalidated":N}`, `401` bad signature or stale timestamp, `400` malformed body or a tag outside the list. Unsigned calls are refused.
`REVALIDATE_HMAC_SECRET` is a shared secret of 32+ characters, separate from every other secret. Never log it.

## What to send, per event

Listings carry only the `catalogue` tag, product pages carry `catalogue` and `product:<id>`. **So send both for anything that can appear in a list.**

| Event                                                                                                                       | Tags                                          |
| --------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------- |
| Product created, edited, activated, archived or deleted (name, slug, description, **price**, **image**, category)           | `catalogue`, `product:<id>`                   |
| Variant created, edited or deleted; **variant price** changed                                                               | `catalogue`, `product:<id>`                   |
| **Availability state** changed (in stock, low stock, out of stock), from stock edits, orders, cancellations or reservations | `catalogue`, `product:<id>`                   |
| Category created, edited, reordered or hidden                                                                               | `catalogue` (add `category:<id>` if you like) |
| Review published, hidden or deleted                                                                                         | `product:<id>`                                |
| Currency set, payment mode, shipping or checkout availability changed                                                       | `store-settings`                              |
| Site configuration published or rolled back                                                                                 | `site-config`                                 |

**Stock changes:** the storefront shows only the three availability states, never exact stock. Send the hook only when the state **changes**, not on every unit sold. That keeps `catalogue` invalidations rare.

## Failure behaviour

- Do not make a write wait for the hook or fail because of it. Call it after the commit, retry a few times with backoff, and log failures.
- If every retry fails, the TTLs above are the safety net.

## Example (Python, httpx)

```python
import hashlib, hmac, json, time
import httpx

def revalidate(tags: list[str], secret: str, url: str = "http://web:3000/api/bff/revalidate") -> None:
    body = json.dumps({"tags": tags}, separators=(",", ":"))
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.{body}".encode(), hashlib.sha256).hexdigest()
    httpx.post(url, content=body, timeout=3.0, headers={
        "content-type": "application/json", "x-nc-timestamp": ts, "x-nc-signature": sig,
    }).raise_for_status()

# after committing a product price change:
revalidate(["catalogue", f"product:{product.id}"], settings.revalidate_hmac_secret)
```
