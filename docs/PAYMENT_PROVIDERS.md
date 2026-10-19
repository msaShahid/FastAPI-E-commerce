# Configurable payment providers: design (not yet built)

Goal: an authorized store administrator configures Stripe and Razorpay (and later others) from an admin
panel, with no code change. NovaCart is single-vendor per deployment, so everything below is
per deployment (one store), not per seller. **Phase 2.8 implements only the groundwork; this document is the
plan for the rest.**

## What exists after Phase 2.8 (verified)
- A `PaymentProvider` interface (`app/core/payments/interface.py`) with `name`, intent create/retrieve/cancel,
  refund, `supports_currency`, and `verify_and_parse_webhook`. `StripePaymentProvider` is the only implementation.
- Provider-specific work (signature verification, event parsing, API calls) lives **inside** the provider class and
  returns a normalised `WebhookEvent`. Shared rules (order lifecycle, refund limits, disputes, reconciliation) live
  in `OrderLifecycleService` / `PaymentService` and never see provider-specific payloads.
- `payments.provider` and `processed_webhook_events.provider` record which provider handled each payment/event,
  and event ids are de-duplicated per provider. Webhook URLs are per provider: `/payments/webhooks/{provider}`
  (`/payments/webhook` stays for Stripe). An unknown provider is 404.
- Credentials still come from environment variables (`STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`).

## Minimum changes before a second provider can be added
1. Resolve the provider **per payment** instead of one global instance: `get_provider(name, store_config)`.
   Payment creation uses the store's active provider; refunds, cancellations and webhooks use the provider
   **recorded on the payment**, never the current setting.
2. Make provider-scoped lookups strict: `payments.provider_payment_id` is unique today across providers; change
   it to unique `(provider, provider_payment_id)` (a small migration) and pass the provider into repository lookups.
3. Give each provider a capability declaration (currencies, partial refunds, dispute events, cancel-intent) in the
   same style as `app/core/money.py`, so the platform never assumes a provider supports everything.
4. A Razorpay class implementing the interface, plus its own signature check (HMAC over the raw body) and event
   mapping into the existing `WebhookEvent` fields. No change to the lifecycle service.

## Provider selection
One row per provider per store in a future `payment_provider_settings` table: `provider`, `status`
(`draft` / `enabled` / `disabled`), `currencies` it may be used for, `mode` (test/live), `credentials_ref`,
`webhook_ref`, `verified_at`, audit columns. A store has one **active provider per currency** (explicit; no implicit
default): at checkout, choose the provider enabled for the order's currency. If none is eligible, payment creation is
refused with a clear error (the same fail-closed rule as today's `PAYMENT_CURRENCIES_ENABLED`).

## Credential storage
- Never in source, env files committed to git, API responses, logs or audit details.
- Stored **encrypted at rest** (application-level envelope encryption: a per-record data key encrypted by a master
  key held outside the database, e.g. a cloud KMS or a secret manager). Only the key reference and key id are in the DB.
- **Write-only** through the admin API: secrets can be set or rotated, never read back (the API returns only
  `configured: true`, the last four characters, `verified_at`).
- Changes require the administrator role, are audited (who/when, never the value), and live mode additionally
  requires the existing `LIVE_PAYMENTS_ENABLED` decision plus an explicit confirmation.

## Enabling and disabling
- Enabling runs a **credential check** (a harmless authenticated call, e.g. retrieve the account) and records
  `verified_at`; a provider cannot be enabled unverified, and not for a currency it does not support.
- **Disabling a provider blocks only NEW payments through it.** Existing payments keep their recorded provider, so
  refunds, cancellations, dispute handling and pending/retried webhooks keep working: the webhook route and the
  credentials needed to verify them stay available while any payment or event for that provider is unreconciled.
  A provider with in-flight payments or refunds can be disabled but not deleted.

## Currency capabilities
Each provider declares which currencies it can charge (verified against its documentation, as `app/core/money.py`
does for Stripe). The store's currency must be supported by an enabled provider; account-level support (which
currencies this merchant account can charge) is confirmed by the administrator per provider.

## Switching providers
Switching is an ordinary change of the active provider per currency. It affects **only new payments**; nothing
existing is rewritten. An in-flight (pending) payment is completed or expired on its original provider. Switching
while a payment is pending is allowed but shown as a warning with the count of open payments.

## Secret rotation
Provider keys and webhook signing secrets are rotated without downtime: add the new secret as `next`, accept
webhooks verified by **either** secret during an overlap window, then retire the old one. Rotation is audited and
verified (a successful credential check) before the old secret is removed.

## Webhook configuration
Each provider has its own URL (`/payments/webhooks/{provider}`) and signing secret. The admin panel shows the URL
to register and the events to subscribe to (payment success/failure, refund updates, disputes) and can verify
reception with a test event. Verification always uses the raw, unmodified body.

## Recommended phase
Build the configurable provider system as its own milestone **after** the payment lifecycle is verified against a
real Stripe test account and the business decisions on refunds/late payments are made: (1) the four minimum changes
above, (2) `payment_provider_settings` + encrypted credentials + admin API, (3) Razorpay, (4) the admin panel. Doing it
earlier would add risk to the one provider that is not yet proven in test mode.
