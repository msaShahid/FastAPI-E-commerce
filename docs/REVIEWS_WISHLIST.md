# Product reviews and wishlists

Migration `e3a7b1c9d452` (additive: two new tables, nothing existing is touched; run `alembic upgrade head`).

## Reviews

Defaults chosen for a store that wants trustworthy reviews with little moderation work. Each is a small change if the client prefers otherwise.

- **Who:** only a signed-in customer who BOUGHT the product: they have an order containing it that is `paid` or `fulfilled` (a variant
  of the product counts; a refunded, cancelled or merely pending order does not). Otherwise `403 review_requires_purchase`.
  Guest purchasers cannot review (there is no account to attach it to).
- **One per customer per product** (`409 already_reviewed`, also under simultaneous requests). The customer may edit or delete their own
  (`PATCH` / `DELETE /products/{id}/reviews/mine`) and may review again after deleting. If the store has hidden it, they cannot edit it
  (`403 review_hidden`) but can still read it.
- **Content:** a 1 to 5 rating, an optional title (100 characters) and optional body (2,000). Plain text only: control characters are
  removed and the text is stored as typed, so **the storefront must escape it** when showing it (never as HTML).
- **Published immediately.** Everyone sees published reviews only. Administrators can hide one (a reason of 3+ characters is required,
  kept with who hid it) or publish it again, or delete it: `GET /admin/reviews?status=&product_id=&rating=`, `PATCH`, `DELETE`
  `/admin/reviews/{id}`. All three are audited.
- **Privacy:** the public sees `display_name`, the first two characters of the username and asterisks ("pr*********"), never an email
  or an account id; every review is marked `verified_purchase`.
- **Reading:** `GET /products/{id}/reviews?sort=newest|highest|lowest` (paginated), `GET /products/{id}/review-summary`
  (count, average to one decimal, distribution 1 to 5) and `GET /reviews/summaries?product_ids=1&product_ids=2` (up to 50 products in
  one query, for the stars on a product list). Reviews of a product that is not on sale answer 404.
- Writes are rate limited (10 per minute). Averages are computed when asked (one grouped query); there is no cached or stored rating on the
  product, so there is nothing to drift.
- Not built: replies from the store, "helpful" votes, photos, report-abuse by customers, email notification of new reviews.

## Wishlist (registered customers only)

`PUT /wishlist/{product_id}` and `DELETE /wishlist/{product_id}` are idempotent and answer `{product_id, in_wishlist, count}`.
`GET /wishlist` lists the saved products that are on sale, most recently saved first, in the same shape as the product list (prices,
variants' range, availability); `GET /wishlist/ids` returns only the ids, to mark saved products on a page. Only products on sale can be
added; a saved product that is later withdrawn stops appearing and returns if the product does. At most 200 products (`409
wishlist_full`); adding one already saved is never an error. Wishlists are private to their owner and are deleted with the account.
