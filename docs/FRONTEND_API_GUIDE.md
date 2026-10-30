# NovaCart: the storefront and admin integration guide

For whoever builds the frontend. Three files go together:

| File | What it is |
|---|---|
| `docs/openapi.json` | Every endpoint with its exact request and response shapes. **Generate typed clients from this** (below). |
| `docs/ERROR_CODES.md` | Every stable error `code` and its HTTP status. |
| this guide | The rules and flows that a schema cannot express. |

Both generated files are checked by a test, so they always match the running API. After the backend changes, regenerate with
`docker compose exec api python -m scripts.export_api_docs` and copy `docs/openapi.json` into the frontend repo.

```bash
npx openapi-typescript docs/openapi.json -o src/api/schema.d.ts      # types for every request and response
```

Interactive docs (`/docs`) exist in development only; they are switched off in staging and production.

## 1. Conventions

- **Base path** `/api/v1`. JSON in, JSON out. Image upload endpoints take `multipart/form-data`.
- **Money** is always an integer in **minor units** of the store currency (paise for INR): fields ending `_cents`. Read the currency and
  how many decimals to show from `GET /store/settings` (`currency`, `minor_unit_exponent`); display `amount / 10**exponent`. Never use
  floating point for money, and never send a price when buying: the server prices everything.
- **Pagination**: `?page=1&page_size=20` returns `{items, total, page, page_size}`. Every list endpoint is paginated or has a documented bound.
- **Errors** are `{"detail": "...", "request_id": "..."}` plus `"code"` when the error has a stable one. **Branch on `code` (or the HTTP
  status), never on the wording of `detail`.** Validation errors (422) have `detail` as a list of field errors. Quote `request_id` to support.
  Rate limited endpoints answer `429`.
- **Auth**: `Authorization: Bearer <access_token>`. A token whose user is not an administrator is refused (`403`) on admin endpoints.
  The OpenAPI file marks some endpoints as bearer-protected that also work without a token (the catalogue, the cart, checkout):
  for those the token is **optional**, and a guest uses cookies instead (section 3).

## 2. Accounts

| Screen | Call |
|---|---|
| Sign up | `POST /auth/register` `{username, email, password}` |
| Sign in | `POST /auth/login` `{email, password}` returns `{access_token, refresh_token}` |
| Stay signed in | `POST /auth/refresh` `{refresh_token}`. The access token lasts 15 minutes and the refresh token 7 days by default. Refresh once on a `401`, then retry the request once. |
| Sign out | `POST /auth/logout` |
| Who am I | `GET /auth/me` (role is `customer` or `admin`) |
| Forgot password | `POST /auth/password/forgot` always answers the same, whether or not the email exists |
| Change password | `POST /auth/password/change` |
| Verify email, change email | `POST /auth/email/verification`, `/auth/email/verify`, `/auth/email/change`, `/auth/email/change/confirm` |

**Pages the emails link to** (the storefront must provide these routes; the token arrives as `?token=`):
`/reset-password` then `POST /auth/password/reset`; `/verify-email` then `POST /auth/email/verify`; `/confirm-email-change` then
`POST /auth/email/change/confirm`. An invalid or expired link answers `400 invalid_or_expired_token`.

## 3. Cookies, CORS and guests (the part that breaks in production)

- A visitor without an account gets an **HttpOnly cookie `guest_cart_token`** the first time they touch the cart, and carts, guest
  checkout and guest order reads work from it. After a checkout as a guest, an order-scoped **HttpOnly cookie `guest_pay_<order id>`**
  (path `/api/v1/payments`) is what lets that guest pay for exactly that order.
- Signing in or registering **merges** the guest cart into the account's cart (same product or variant: quantities add up).
- Every request that may set or use these cookies must send credentials: `fetch(url, { credentials: "include" })`.
- Cookies are `SameSite=Lax` (and `Secure` in staging and production). So **serve the storefront and the API from the same site**, for
  example `shop.example.com` and `api.example.com` (or the same host behind one proxy). A different registrable domain will not send them.
- The API only accepts browser calls from the origins in `CORS_ORIGINS` (never `*`). Add the storefront's exact origin there.
- Emailed links use `FRONTEND_BASE_URL` (https is required in staging and production).

## 4. The catalogue

`GET /store/settings` first: currency, shipping and free-shipping threshold, how long an unpaid order is held, and
`checkout_available` / `payments_available` with `unavailable_reasons` (use these to disable the buy button instead of letting checkout fail).

- `GET /categories`, `GET /products?category_id=&search=&min_price=&max_price=&sort=` with `sort` one of `created_at`, `price_cents`,
  `name` (a leading `-` reverses). `GET /products/{id}` for the detail. Public.
- **Stock is hidden.** Show `availability` (`in_stock`, `low_stock`, `out_of_stock`) and `available`; `stock` is `null` for shoppers.
- **Products with options** (`has_variants: true`): `price_cents` is the **lowest** price. Show "from" when `has_price_range` is true, using
  `price_from_cents` and `price_to_cents`. The detail has `options` (`[{name, values}]`) and `variants`: each has `id`, `sku`, `options`
  (the chosen words), `price_cents` (what it really costs), `available`, `availability`. Combinations that do not exist are simply
  not in `variants`: grey out or hide them. The shopper picks a combination; **you send that variant's `id`**.
- Price filters and sorting use real variant prices. Reviews and ratings: section 7.

## 5. Cart and checkout

1. **Add**: `POST /cart/items` with `{variant_id, quantity}` for a product with options, or `{product_id, quantity}` for a simple one.
   `422 variant_required` means you sent a product that needs a variant; `422 variant_unavailable` means that option cannot be bought.
   Never send a price.
2. **Show the cart**: `GET /cart`. Each line has `variant`, `quantity`, `price_cents_snapshot`, `current_price_cents`, `price_changed`,
   `line_total_cents`, `available` and `unavailable_reason` (`unavailable`, `out_of_stock`, `insufficient_stock`). Show unavailable lines
   as such and block checkout until the shopper removes or reduces them. `cart_digest` is a fingerprint of the lines (you may ignore it).
3. **Change or remove**: `PATCH /cart/items/{item_id}` `{quantity}`, `DELETE /cart/items/{item_id}`, `DELETE /cart`.
4. **Checkout**: `POST /orders/checkout` with the header **`Idempotency-Key`** (required: generate a UUID per purchase attempt) and a
   body of either `{address_id}` (signed in) or `{shipping_address: {...}}`; a guest must also send `guest_email`.
   - **Retrying the same purchase** (a timeout, a double click): send the **same key** again and you get the same order, charged once.
   - **A new purchase** needs a **new key**. Reusing a key with a different address or a different cart answers `409 idempotency_key_reused`.
   - `409 price_changed`: a price moved since the shopper last saw it. The body has `changes` (with `variant_id` and `sku` for variants)
     and `new_subtotal_cents`. **Nothing was charged and stock was not touched; the cart was already updated.** Show the new total and
     let them confirm by checking out again (same key is fine).
   - `422` for anything that cannot be sold (`variant_unavailable`, an inactive product, not enough stock: `detail` says how many are left
     when only a few remain). Nothing is taken.
   - `503` means checkout is not configured; check `checkout_available` from `/store/settings` beforehand.
5. The `201` response is the **order**: items (each with its frozen `sku`, `variant.options` and price), `subtotal_cents`, `shipping_cents`,
   `tax_cents`, `total_cents`, `currency`, `status`, `shipping_address`.

## 6. Paying, orders and shipping

- **Pay**: `POST /payments` `{order_id}` returns the payment (`client_secret`, `provider`, `amount_cents`, `currency`). Complete it with the
  provider's own browser SDK, using the `provider` field to choose. A guest pays with the cookie from checkout; a signed-in customer with
  their token. **Never mark an order paid from the browser**: the provider confirms to the server by webhook, and the order's `status`
  changes to `paid` a moment later, so **poll `GET /orders/{id}`** (guests: `GET /orders/guest/{id}`) until it does.
- **Statuses**: `pending` (unpaid; cancelled automatically after `pending_order_timeout_minutes`), `paid`, `fulfilled` (shipped),
  `cancelled`, `refunded`. A customer may cancel only a `pending` order: `POST /orders/{id}/cancel`.
- **Tracking**: an order's `shipments` lists parcels (carrier, tracking number, a tracking link, delivered or not) once the store enters them.
- **Orders list**: `GET /orders`. Saved addresses: `/addresses`.
- Live payments stay off (`LIVE_PAYMENTS_ENABLED=false`) until the store's payment accounts are configured: until then `payments_available`
  is false.

## 7. Reviews and wishlists

- `GET /products/{id}/reviews?sort=newest|highest|lowest`, `GET /products/{id}/review-summary`, and for the stars on a list
  `GET /reviews/summaries?product_ids=1&product_ids=2` (up to 50). **Review text is plain text: always escape it, never render it as HTML.**
- A signed-in customer can review a product only after buying it (`403 review_requires_purchase` otherwise): show "write a review" only
  then; `409 already_reviewed` means edit instead (`PATCH /products/{id}/reviews/mine`).
- Wishlist (signed in): `GET /wishlist/ids` to colour the hearts, `PUT` and `DELETE /wishlist/{product_id}`, `GET /wishlist` for the page.

## 8. The admin area (administrators only)

| Area | Endpoints |
|---|---|
| Catalogue | `/categories`, `/products` (create, edit, images; `stock` can only be changed with a `stock_reason`) |
| Options, variants, stock | `/products/{id}/options`, `/variants`, `/variants/bulk`, `/variants/missing-combinations`, `/stock`, `/stock-history`, `/convert-to-variants` |
| Orders and shipping | `GET /orders/admin`, `PATCH /orders/{id}/status`, `/orders/{id}/shipments`, `/admin/orders/{id}/cancel-and-refund`, `/payments/{id}/refund` |
| Payments | `/admin/payment-providers/...`, `/admin/refunds`, `/admin/reconciliations`, `/admin/disputes`, `/admin/webhook-events` |
| Reports | `/admin/reports/summary`, `sales-by-day`, `top-products?group_by=variant`, `low-stock` |
| Reviews | `/admin/reviews` (hide with a reason, publish, delete) |
| Tax and GST set-up | `/admin/tax/...` (a second administrator must approve each record) |
| Store and email | `/store/currency`, `/admin/email-providers/...` |
| Audit | `/audit-logs` |

The product detail for an administrator additionally has exact `stock`, `base_price_cents`, each variant's `status` and exact `stock`, and the
ids needed to rename or remove option values (`options[].id`, `options[].value_ids`). Every change to a product's options or variants answers with the
updated product, so a screen can redraw from the response.
