# Operations runbook

Written for a single-store deployment (one database, one Redis per store). **Backup, restore and secret-rotation commands below
have not been run against your infrastructure: rehearse them on a copy before you need them.**

## 1. What to watch

| Signal | Where | Alert when |
|---|---|---|
| API alive | `GET /health` (public, `{"status":"ok"}`) | not 200 |
| Database reachable | `GET /health/db` (public) | 503 |
| Database, Redis, worker | `GET /api/v1/admin/health/dependencies` (admin) | `status` is not `ok` (worker: no job finished within 3x the sweep interval, minimum 3 minutes) |
| Expiry and refunds | `GET /api/v1/admin/lifecycle/health` (admin) | `sweep_overdue`, `failed_jobs` > 0, `stale_pending_orders` > 0, `active_refunds.uncertain` > 0 |
| Money needing a human | `GET /api/v1/admin/reconciliations` (admin) | any open record (see section 3) |
| Failed webhooks | `GET /api/v1/admin/webhook-events?status_filter=failed` | any entry that keeps `attempts` rising |
| Startup warnings in logs | log line `FORWARDED_ALLOW_IPS is not set` | present (every client shares one rate-limit bucket) |

The worker (`python -m scripts.run_worker`, the `worker` service) is **required**: without it orders never expire, refunds are never
sent and confirmation emails are never produced. It serves no HTTP, so watch it through the admin endpoints above.

Logs are JSON with a `request_id`; secrets and personal data are redacted (see `docs/logging.md`). Customers receive the request id
with every error; search for it.

## 2. Backups and restore

Postgres holds all state; the `media` volume holds uploaded images. Redis holds only cache and rate-limit counters (safe to lose).

```
# backup (database), from the host running docker-compose.prod.yml
docker compose -f docker-compose.prod.yml exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > novacart-$(date +%F).dump

# restore into an EMPTY database (a new volume or a freshly created database), then bring the app up
docker compose -f docker-compose.prod.yml exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner' < novacart-YYYY-MM-DD.dump
```

* Take a backup **before every migration**. Recovery from a bad release is restore, not `alembic downgrade` (`docs/MIGRATIONS.md`).
* Store backups off the host, encrypted, and test a restore regularly: an untested backup is not a backup.
* Back up the `media_prod` volume with your host's volume snapshot tool.
* After a restore, run the checks in section 1 and look at `/admin/reconciliations`: payments that completed at the provider
  after the backup was taken must be reconciled by hand against the provider dashboard.

## 3. Reconciliation records (`/admin/reconciliations`)

Each record is something the system refused to guess about. Open records mean money may be wrong. Kinds you will meet:

| Kind | Meaning | Do |
|---|---|---|
| `amount_mismatch` | A provider reported a payment whose amount or currency differs from the order | Check the provider dashboard; do not fulfil |
| `unmatched_payment` | A payment event for a payment we have no record of | Find it at the provider; link or refund by hand |
| `cancel_uncertain` | An intent could not be cancelled (Razorpay cannot cancel; or a provider call failed) | Confirm at the provider that it is unpaid |
| `refund_uncertain` | A refund request's outcome is unknown after retries | Check the provider before requesting another |
| `refund_blocked_by_dispute` | A queued refund was NOT sent because a dispute opened first | After the dispute closes, request a new refund |
| `dispute_during_refund` | A dispute and a refund overlapped; the real outcomes are recorded | Review with the provider |
| `dispute_opened`, `unmatched_dispute` | A dispute exists | Respond at the provider within its deadline |

## 4. Secrets and rotation

* **`JWT_SECRET`**: rotating it signs everyone out (access and refresh tokens stop verifying). Change it in `.env.production`, restart
  `api` and `worker`. The old shared value must be treated as compromised.
* **Provider API keys and webhook secrets** (admin API `/api/v1/admin/payment-providers/...`): rotate there; payments are paused
  while a configuration is invalid, and a rotated-away webhook secret keeps verifying during an overlap window. Use test keys until
  section 5 is complete.
* **`PROVIDER_SECRETS_KEY`** (encrypts stored provider credentials): **no key-rotation procedure exists in this codebase.** Changing
  it makes stored credentials unreadable; you would re-enter every provider configuration. Do not change it casually.
* **Database password**: change `POSTGRES_PASSWORD` and `DATABASE_URL` together; the Postgres volume keeps the old password until you
  `ALTER USER` inside the database.
* The application refuses to start in staging/production with placeholder secrets.

## 5. Before taking real payments (all must be true)

1. `LIVE_PAYMENTS_ENABLED` stays `false` until every item here is done and recorded.
2. **Stripe test-mode acceptance** (`docs/STRIPE_TESTMODE_VERIFICATION.md`, `scripts/stripe_testmode_check.py`): **not yet performed.**
3. **Razorpay sandbox acceptance: not performed**, and the adapter was written from secondary sources. Keep Razorpay disabled until it
   has been checked against Razorpay's official documentation and sandbox (receipt length, refund idempotency header, signature and
   event behavior).
4. Store currency chosen and confirmed (it cannot be changed once set).
5. Shipping and tax configured deliberately (`FLAT_SHIPPING_CENTS`, `TAX_RATE_PERCENT`: 0 is not an approved rate).
6. A real email sender configured: `EMAIL_BACKEND=smtp` with `SMTP_HOST`, `SMTP_USERNAME`/`SMTP_PASSWORD`, `EMAIL_FROM` and
   `FRONTEND_BASE_URL` (https). With `EMAIL_BACKEND=log` nobody receives password-reset links or order emails. Send yourself a
   password reset from the real storefront and confirm it arrives (check spam and SPF/DKIM for the sending domain).
7. TLS-terminating reverse proxy in place; `FORWARDED_ALLOW_IPS` set to its address; API port reachable only through it.
8. Backups running and a restore rehearsed; monitoring on section 1.
9. At least two active administrators (the last one cannot be demoted or deactivated).

## 5a. Account emails

Password reset, email verification, email-change confirmation and security notices are plain-text emails sent by the background
worker (so the worker must be running). Links point at `FRONTEND_BASE_URL`: `/reset-password`, `/verify-email` and
`/confirm-email-change`, each with `?token=...` (the storefront must provide those pages). A reset link lasts 60 minutes, a
verification link 24 hours, an email-change link 60 minutes; each works once. At most 3 reset emails per account per hour.
In development (`EMAIL_BACKEND=log`) the whole email, link included, is written to the worker's log:
`docker compose logs worker --tail 30`. If delivery fails the job retries with backoff; failed jobs show in
`/admin/lifecycle/health` (`failed_jobs`).

### Choosing the email service (administrator API)

An administrator can pick the email service without touching the server's environment: Google (Gmail / Workspace), Zoho Mail or
Mailtrap. Hosts and ports are fixed presets (an administrator cannot point the server at an arbitrary host).

1. `GET /api/v1/admin/email-providers/catalog`: the three services, their fields and setup notes.
2. `PUT /api/v1/admin/email-providers/{google|zoho|mailtrap}`: save the settings (`username`, `password`, `from_email`,
   `from_name`, plus `mode`: the Zoho data centre `com|in|eu|com.au|jp`, or Mailtrap `sandbox|live`). Credentials are encrypted
   and never returned. **This needs `PROVIDER_SECRETS_KEY`** (generate with
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`); without it the call answers 503.
3. `POST .../{provider}/test`: connects and signs in, sending nothing. `POST .../{provider}/test-email`: sends one message to
   YOUR OWN account address.
4. `POST .../{provider}/activate`: makes it the service for every email. A live test must pass first. Switching to another
   service is the same call, and takes effect for the next email with no restart. `POST .../deactivate` returns to the
   server's `EMAIL_BACKEND` settings (the fallback whenever none is active).
5. `GET /api/v1/admin/email-providers` shows which service is in use, each one's last test, last successful send and last failure
   code. A failing service shows `last_error_code` (for example `auth_failed`) without reading logs.

Service notes: **Google** needs an *App password* (2-Step Verification on); the From address must be the account's own or a
verified alias. **Zoho**: choose the data centre the account lives in (India accounts: `in`). **Mailtrap `sandbox`** captures
mail in a test inbox and delivers NOTHING to customers (development and demos only); `live` delivers from a verified domain.
Changing the credentials of the ACTIVE service is tested first and refused if the test fails, so a typo cannot stop all email.
Losing or changing `PROVIDER_SECRETS_KEY` makes stored credentials unreadable: email jobs then fail loudly (they never fall back
to another account) until the credentials are entered again.

## 6. Known operational limits

* Timestamps are naive UTC (`DateTime` without time zone, server default `now()`): the database server must run in UTC.
* Migrations on large tables have not been reviewed for lock time (`docs/MIGRATIONS.md`).
* Uploads are admin-only and capped at `MAX_IMAGE_BYTES` (default 5 MB) after the request body has been received.
* Rate limits are per client address and shared through Redis; if Redis is down they degrade to a per-process fallback.
