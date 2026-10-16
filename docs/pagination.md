# Pagination limits

Done when: no endpoint accepts an unbounded `page_size`, and no list can grow
without a bound. All the numbers live in one file, `app/shared/limits.py`.

| Limit | Value | Where it applies |
|---|---|---|
| `MAX_PAGE_SIZE` | 100 | `page_size` on every paginated REST list and the GraphQL `users` query |
| `MAX_PAGE` | 10 000 | `page` on the same endpoints |
| `MAX_ADDRESSES_PER_USER` | 20 | saved addresses (the list is not paginated) |
| `MAX_CART_LINES` | 50 | different products in one cart, guest or logged in |
| `MAX_GRAPHQL_ALIASES` | 10 | aliases in one GraphQL request |
| `MAX_GRAPHQL_DEPTH` | 10 | nesting depth of one GraphQL query |

Out of range values are rejected with 422 (REST) or a GraphQL error; they are
not silently clamped, so a client never gets a different page than it asked for.

## Why each limit exists

* **page_size** bounds the rows, memory and JSON size of one response.
* **page** matters because the lists use `OFFSET`. The database still walks past
  every skipped row, so a page far out costs far more than page 1. Without a
  cap, `page=10**18` even overflowed the 64-bit `OFFSET` and returned a 500.
  `100 x 10 000` still reaches a million rows, which is more than anyone
  pages through by hand; export jobs should use a filter instead.
* **GraphQL aliases and depth.** `page_size` is capped per field, but one
  request can repeat a field (`a1: users(...) a2: users(...) ...`) and multiply
  the cap. Aliases are limited per request. The depth limit costs nothing now
  (the schema has no nested object types) and means a future recursive type
  such as user -> orders -> user is limited from day one.
* **Addresses and cart lines** are returned whole, not paginated, so their
  size is bounded by a cap instead. Exceeding a cap returns 409 with the limit
  in the message. Adding more of a product already in the cart is still allowed.
* **Free-text filters** (`search`, audit `action`, `target_type`) have a
  `max_length`, so a filter cannot be a megabyte of text.

## Known limits of the limits

* Two simultaneous "create address" requests can overshoot the cap by one.
  The cap exists to keep the list bounded, not to count exactly.
* Merging a guest cart into a user's cart on login is not capped, so a cart can
  reach at most about twice `MAX_CART_LINES`. It is still bounded.
* Deep OFFSET is capped, not removed. If a list ever needs to be walked in
  full, switch it to keyset pagination (`WHERE id > :last ORDER BY id`).

## Adding a new list endpoint

1. Take `PageParams` (or `ProductQueryParams`) as a dependency. Do not declare
   your own `page` / `page_size`.
2. If it cannot be paginated, cap how many rows can exist and add the
   endpoint to `BOUNDED_UNPAGINATED` in `tests/integration/test_pagination_bounds.py`.
3. Give any free-text query parameter a `max_length`.

`tests/integration/test_pagination_bounds.py` reads the OpenAPI schema, so a new
GET list endpoint that is neither paginated nor allow-listed fails the suite.
