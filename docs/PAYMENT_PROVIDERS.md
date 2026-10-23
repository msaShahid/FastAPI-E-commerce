# Configurable payment providers (Phase 2.10)

Authorized administrators configure, validate, enable, disable and select payment providers through an admin API, with no code
change. Stripe and Razorpay are supported. **Neither is production-ready** (see "Verification status").

## Tenancy: what "store isolation" means here
NovaCart has **no multi-tenant model**: one deployment (its own database, Redis, secrets and Stripe/Razorpay accounts) serves exactly one
client's single-vendor store; `store_settings` is a singleton. Provider configuration therefore lives in that deployment's database and
**no endpoint takes a store, tenant or organization identifier** (tested: the OpenAPI schema has none, and unknown fields/query parameters
are rejected or ignored). A client administrator can only ever see and change their own deployment's configuration; separation between
clients is separation of deployments. Authorization is the existing `admin` role. If a shared multi-store deployment is ever built, every
table here needs a store key and every query a store filter: that is a different project, not assumed here.

## Verification status (do not read more into this than it says)
| | Stripe | Razorpay |
|---|---|---|
| Unit tests | yes | yes |
| Webhook signature verification and parsing | **real signatures** through the real code | HMAC checked against a locally computed signature (mocked world) |
| Outbound API calls (create, cancel, refund, validate) | **faked**; not verified against Stripe | **mocked HTTP** (`httpx.MockTransport`); not verified against Razorpay |
| Real sandbox/test-mode run | **NOT performed** (runbook: `docs/STRIPE_TESTMODE_VERIFICATION.md`) | **NOT performed** |
| Written from official documentation | SDK-based | **No**: built from secondary sources; must be checked against Razorpay's official docs |
| Production review | not done | not done |
Entering credentials never enables a provider. A LIVE configuration can only be enabled after the administrator records that sandbox
verification and a production review were done (an auditable statement; NovaCart cannot verify it itself), a correctly signed webhook has
been received, `LIVE_PAYMENTS_ENABLED=true`, and the credentials validated.

## Provider selection
`payment_provider_configs`: one row per (provider, mode) with status `draft | enabled | disabled`, the enabled currencies, and at most one
**preferred** configuration (database-enforced; only an enabled one). A NEW payment uses the preferred configuration if it is enabled,
its credentials are validated, the order's currency is among its enabled currencies **and** the platform-verified capabilities of that
provider, and (for live) live payments are allowed. Otherwise the payment is **refused** with a code (`no_payment_provider_selected`,
`provider_disabled`, `provider_credentials_not_valid`, `provider_currency_not_supported`, `provider_mode_not_allowed`). There is **no
automatic failover**: a failing or ineligible provider never causes another to be used, and a second payment for the same order is never
started automatically. A payment request cannot choose the provider or any store (extra fields are ignored). Until the first
configuration exists, the environment-configured Stripe (`STRIPE_*`, `PAYMENT_CURRENCIES_ENABLED`) works exactly as before.
Every payment records its provider and the configuration that created it (`payments.provider`, `provider_config_id`, and
`provider_reference` for providers with a second reference, e.g. Razorpay's `pay_...`).

## Credential storage
- Credentials and webhook secrets are encrypted with Fernet (`cryptography`) before storage. The database holds ciphertext, a non-secret hint
  (last 4 characters) and statuses only. A database dump/backup alone does not reveal them.
- **Limitation:** the master key (`PROVIDER_SECRETS_KEY`) is an environment variable. Anyone who can read both the database and the
  application's environment can decrypt. That is **not** a KMS/HSM boundary. For production, inject the key from a secrets manager or cloud
  KMS at deploy time and restrict who can read the environment; envelope encryption with a KMS-held key is the recommended next step.
- With no key configured the API refuses to store credentials (503): there is no plaintext fallback.
- Secrets are write-only: no endpoint, log line, audit detail, error message or validation error returns one (tested across the whole flow).
  Validation errors on these routes do not echo submitted input.
- Master-key rotation: set the new key and a new `PROVIDER_SECRETS_KEY_ID`, keep the old one in `PROVIDER_SECRETS_PREVIOUS_KEYS`
  (`id:key,id:key`), re-save/rotate credentials to re-encrypt, then drop the old key. (A bulk re-encryption command is not built.)
Generate a key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`

## API contract (all administrator-only, under `/api/v1/admin/payment-providers`)
| Method and path | Purpose |
|---|---|
| `GET /catalog` | supported providers, credential fields, **verified capabilities** (currencies, cancel, full refund, disputes), verification note |
| `GET /` and `GET /{id}` | sanitized configurations with readiness (`ready_for_use`, `ready_for_live`, blockers) |
| `POST /` | add `{provider, mode: test or live, credentials, webhook_secret?, currencies?}`; encrypted; creates a **draft** |
| `PUT /{id}/credentials` | rotate credentials and/or webhook secret (write-only); **pauses new payments** until validated and enabled again; old webhook secret keeps verifying until retired |
| `POST /{id}/validate` | harmless authenticated call to the provider; safe reason code; checks the key belongs to the declared mode |
| `PUT /{id}/currencies` | enabled currencies, a subset of the provider's verified capabilities |
| `POST /{id}/enable`, `/disable` | enable only when ready (409 `provider_not_ready` lists blockers); disable blocks NEW payments only |
| `POST /{id}/prefer` | make it the provider for new payments (store currency must be supported); existing orders/payments/refunds untouched |
| `POST /{id}/attestations` | record `sandbox_verified` / `production_review` with a reference (required for live) |
| `POST /{id}/webhook-secret/retire-previous` | end the rotation overlap |
| `GET /{id}/webhook` and `/readiness` | URL and events to register at the provider and verification state; activation blockers |
Webhook URLs: `/api/v1/payments/webhooks/{provider}` (and `/api/v1/payments/webhook` for Stripe, unchanged). The URL only names a provider: an
event is accepted only if its signature verifies with one of **that provider's own stored secrets** (disabled configurations and a rotated-away
secret included); events are de-duplicated per (provider, event id); a payment is only ever found by its own provider's references.
Razorpay webhooks have no timestamp, so replay is bounded by de-duplicating `x-razorpay-event-id` (or a hash of the body), not by time.

## Enabling and disabling
Enable requires validated credentials and at least one supported currency; **live** additionally requires `LIVE_PAYMENTS_ENABLED=true`, a webhook
that has delivered a correctly signed event, and the two attestations (sandbox verification, then production review). Database constraints
re-enforce this (enabled needs valid credentials; live-enabled needs the webhook and attestations; one preferred, only if enabled).
**Disabling blocks new payments only.** The configuration and its credentials are kept (never deleted: payments reference them), so existing
payments still reconcile: webhooks verify through it, refunds and cancellation use the configuration that created the payment, and recovery
works. Disabling the preferred provider leaves the store with **none** until an administrator chooses one.

## Currency capabilities
Declared per provider in `app/core/payments/registry.py`: Stripe = the platform-verified set; **Razorpay = INR only** until others are verified.
Amounts are integers in the currency's minor unit for both (one conversion function). Capabilities are explicit, not assumed: Razorpay cannot
cancel an order (`supports_cancel=false`: expiry records `unsupported_cancel`, and a later payment is only reconciled, never reopened), and
its refund needs the payment reference (`pay_...`) learned from the capture webhook. The default currency remains INR, configurable by an
administrator, and every order/payment keeps its own currency.

## Switching providers
Changing the preferred provider affects **new** payments only. Existing orders, payments, refunds, disputes and webhook records are never
rewritten (tested). A pending payment is continued and refunded through the provider that created it, even if another is preferred now; a
retry of the same order never creates a second payment.

## Secret rotation
Rotating credentials resets their validation and clears sandbox/production attestations (evidence for the old credentials does not carry
over), bumps `version`, and pauses new payments on that configuration. Rotating the webhook secret keeps the old secret verifying until
retired, so deliveries in flight are not lost. Existing payments keep reconciling with the new credentials: they must belong to the same
provider account, or reconciliation of those payments will fail (shown as a degraded, invalid configuration).

## Webhook configuration
`GET /{id}/webhook` returns the URL, the events to subscribe to, and whether a correctly signed event has been received (`verified`).
Verification happens by receipt: send a test event from the provider's dashboard/CLI. Subscribe Stripe to payment_intent.succeeded/payment_failed,
refund events and charge.dispute.*; Razorpay to payment.captured, payment.failed, refund.processed, refund.failed. Razorpay disputes are not handled.

## Remaining blockers and limits
1. Stripe test-mode verification and a Razorpay sandbox verification are **not performed**; the Razorpay adapter must be checked against its
   official documentation first.
2. The master key is environment-held, not KMS-held.
3. No automatic failover, by design (needs its own approved design).
4. Razorpay: orders cannot be cancelled; no dispute handling; INR only; payment references arrive via webhook, so a refund before the capture
   webhook is refused.
5. No admin UI (backend contract only).
6. `payments.provider_payment_id` is still globally unique; provider scoping is applied in every lookup, but a provider id collision across
   providers would be rejected at insert rather than stored.

## Recommended phase
Next: (1) run the Stripe test-mode runbook and a Razorpay sandbox verification, recording the attestations; (2) KMS-backed master key and a
bulk re-encryption command; (3) an admin UI against the contract above; (4) per-provider unique `(provider, provider_payment_id)`.
