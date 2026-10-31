# Coupons

Migration `a8d2f6c1e045` (additive: a `coupons` table and discount columns on `orders` and `order_items` that default to 0 / NULL, so every
existing order means exactly what it did).

## The rules (this store's defaults)

| Rule | Behaviour |
|---|---|
| Kinds | `percentage` (percent of the goods, optionally capped by `max_discount_cents`), `fixed` (an amount off the goods, never more than they cost), `free_shipping` (the shipping charge is waived) |
| One per order | A checkout takes one `coupon_code`; coupons do not stack |
| Codes | 3 to 32 characters, letters, digits, `_` and `-`; stored in capitals; customers may type any case; unique ignoring case |
| What it applies to | The goods of the whole cart (no per-product or per-category restriction yet). Shipping is only affected by a free-shipping coupon |
| Conditions | `min_subtotal_cents` on the goods, `starts_at` / `ends_at`, `active`, `max_uses_total`, `max_uses_per_customer` |
| What counts as a use | An order that carries the coupon and is still **alive**: pending, paid or shipped. A cancelled, expired or refunded order gives its use back. Nothing is counted in a field that could drift |
| Who is "a customer" | A registered account, or a guest's email address (compared ignoring case) |
| Simultaneous orders | The coupon row is locked while its limits are counted and the order is created, so two checkouts cannot both take the last use |
| Free-shipping threshold | Counts what the customer pays for the goods, so a discount can bring an order back below it |

## What the customer sees

* `POST /cart/coupon/preview` `{code, guest_email?}`: what the code would do to the cart now (discount, new shipping, goods after discount). Nothing is reserved.
  Rate limited. A code that cannot be used says why: `coupon_invalid` (does not exist, switched off, not started or ended: one answer, so nothing is revealed
  about codes the customer did not already have), `coupon_minimum_not_met` (with `minimum_cents`), `coupon_limit_reached`, `coupon_not_applicable`.
* `POST /orders/checkout` takes the same `coupon_code`. It is checked again there; if it fails the whole checkout is refused and nothing changes.
  The idempotency key covers the coupon: the same key with a different (or no) coupon answers `409 idempotency_key_reused`.
* The order shows `discount_cents` and `coupon_code`; each item shows its share (`discount_cents`). **total = subtotal - discount + shipping + tax.**

## Tax, invoices and reports

* With the GST calculator on, tax is worked out on the price **after** the discount: the discount is shared over the lines in proportion to their value
  (exactly, to the paisa) and reduces each line's taxable value. With inclusive prices the customer pays the discounted price and the GST inside it falls
  accordingly. A tax invoice shows the goods before discount, the discount and the coupon code. (Whether a coupon counts as a discount under section 15
  of the GST Act, which depends on how it is given, is for your tax professional to confirm.)
* Reports: `goods_cents` is the goods **as customers paid for them (after discounts)**, so `gross = goods + shipping + tax` still holds; `discount_cents`
  reports what coupons took off; top-products revenue is net of discounts.
* An order's discount is **frozen** (the database refuses to change it), together with a snapshot of the coupon's terms.

## Administration (`/admin/coupons`, administrators only)

`POST` create, `GET` list (filter `state` = active, disabled, scheduled, expired, exhausted; `search`), `GET /{id}` (with `uses` and `state`),
`PATCH /{id}`, `DELETE /{id}`, `GET /{id}/redemptions` (the orders that used it, whatever their status). What a coupon **gives** (kind, percentage,
amount, cap) cannot be changed, and it cannot be deleted, once an order has used it (`409 coupon_in_use`): switch it off (`active: false`) and create a new one.
Every change is audited.

## Not built

Per-product or per-category coupons, coupons for specific customers, automatic discounts without a code, stacking, buy-X-get-Y, and gift cards.
