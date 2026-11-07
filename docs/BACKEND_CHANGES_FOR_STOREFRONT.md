# Backend changes made for the Next.js storefront

Additive and backwards compatible. Deploy order: run `alembic upgrade head` (two new revisions), then set the optional variables below.

| Area | What | New settings (all optional) |
| --- | --- | --- |
| Error codes | `insufficient_stock` (+`available` when public), `product_unavailable`, `cart_full` (+`limit`), `email_already_registered`, `username_taken`; error handlers can carry small structured extras | |
| Cart | `product.image_url` on cart lines | |
| Refresh tokens | A replay inside a grace window gets a sibling token (audited); outside it, reuse still revokes the family. Migration `d4f8a1b6c3e9` adds `refresh_tokens.rotated_at`, `grace_replays` | `REFRESH_REUSE_GRACE_SECONDS=10` (0 = strict), `REFRESH_REUSE_GRACE_MAX_REPLAYS=3` |
| Quote | `POST /cart/quote`: what checkout would charge (read-only, same rules, parity-tested against real checkouts) | |
| Payments | `GET /store/payment-config` (public key only), `PUT /admin/payment-providers/{id}/publishable-key` | `STRIPE_PUBLISHABLE_KEY` (only while no provider is stored) |
| Site config | `GET /site-config`, `/admin/site-config/*` (draft, publish, versions, rollback). Migration `e7b2c5a9d041` adds two tables | |
| Cache hook | After a commit, a signed call tells the storefront what is stale (`docs/REVALIDATE_HOOK.md`) | `FRONTEND_REVALIDATE_URL`, `FRONTEND_REVALIDATE_SECRET` (32+ chars, set together) |
| Deployment | `.env.example`, `.env.production.example`, `GRAPHQL_ENABLED` switch (default on) | `GRAPHQL_ENABLED=false` in production |

Existing tests changed: `tests/auth/conftest.py` (fake repository gained the new methods; `strict_refresh` fixture) and three reuse tests in `tests/auth/test_auth_service.py` that now run with the grace window off.
Not announced to the storefront on purpose: bulk stock decrements at checkout (the storefront shows availability states and its cache lifetime covers them).
Not covered by a test: the GST engine's `requires_address` path of the quote (the flat-rate engine is covered).

## M6 admin: staff roles and permissions (BE-P1)

Migration `c5e1a7f3b964` (after `e7b2c5a9d041`): tables `staff_roles`, `staff_role_assignments`, system roles Owner and No access; every existing administrator gets Owner. All 126 administrator routes, the staff paths of `/users`, admin reads of orders and invoices, and draft-product visibility now check a permission (403 `permission_denied` + `permission`). New: `GET /access/me`, `/admin/permissions`, `/admin/roles*`, `/admin/staff*`. Full rules and the permission-to-endpoint table: `docs/STAFF_PERMISSIONS.md`. The 403 handler now passes error extras through. Tests: `tests/roles/test_permission_coverage.py` (every admin route declares a permission), `tests/integration/test_staff_roles.py`.
