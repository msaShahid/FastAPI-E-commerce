# Launch checklist

What is built and tested is in `docs/STATUS.md`. This is what must be true **before real customers pay**. Items marked **(you)** cannot be
done from the code.

## Must be done

- [ ] **(you)** Run the full test suite in Docker on your machine: `docker compose exec api pytest -q`. It passed in the development
      environment used to build it (PostgreSQL 16, Redis); Docker, CI and your data are not yet proven.
- [ ] **(you)** Production database is **PostgreSQL 15 or newer** (variants need it). Take a backup, run `alembic upgrade head`, and
      check `alembic current` shows the latest revision. Read `docs/VARIANTS.md` before the first deployment of the variants migrations.
- [ ] **(you)** Payment accounts: create the Stripe (and, if used, Razorpay) accounts, set the keys through the admin provider screens,
      run each provider's readiness check, and complete a real test-mode payment, a refund, and a webhook delivery end to end. Until
      then `LIVE_PAYMENTS_ENABLED` must stay `false`. **Payments have only been tested against a fake provider.**
- [ ] **(you)** Email: configure a real email service in the admin email settings and send yourself a password reset and a verification email.
- [ ] **(you)** GST: the GST calculator and tax invoices are built (`docs/GST.md`) but OFF by default; the store uses an interim flat rate until you
      switch it on. Have your tax professional confirm the assumptions listed in `docs/GST.md`, enter the registration, rates per HSN, configuration and
      business profile (two administrators approve each), then set `TAX_CALCULATION_ENABLED=true` and `TAX_CLASSIFICATION_REQUIRED=true`.
      Not covered: B2B/GSTIN invoices, exports, e-invoicing (IRN) and e-way bills.
- [ ] **(you)** HTTPS in front of the API, `FORWARDED_ALLOW_IPS` set to the proxy, `CORS_ORIGINS` and `FRONTEND_BASE_URL` set to the real
      storefront (https), and the storefront and API on the same site (see the guide, section 3).
- [ ] **(you)** Secrets: strong random `JWT_SECRET` and `PROVIDER_SECRETS_KEY`, real database and Redis passwords, none of the example values.
- [ ] **(you)** Backups: schedule database backups and **restore one** onto a scratch database to prove it works.
- [ ] Create the first administrator with `scripts/bootstrap_admin.py`; delete any `__seed copy.py` or other stray script files from the project.
- [ ] Never run the demo seed against production (it refuses, but do not rely on that).

## Strongly recommended

- [ ] Apply the stock-ledger role split in `docs/VARIANTS.md`, so the application database role cannot alter the stock record.
- [ ] Monitoring: watch `GET /health`, `GET /health/db` and the admin `/admin/lifecycle/health` and `/admin/health/dependencies` endpoints; alert on
      open reconciliations (`/admin/reconciliations`) and on webhook failures.
- [ ] Load test the checkout path with realistic concurrency on production-like hardware (concurrency is tested for correctness, not for load).
- [ ] Write the privacy policy, terms and returns policy the storefront must show; decide the returns process (not yet built).

## Not built yet (decisions needed)

| Feature | What is needed |
|---|---|
| Coupons and discounts | Which discount types, and whether they can be combined |
| Customer returns | The returns policy: window, who pays, refund versus replacement |
