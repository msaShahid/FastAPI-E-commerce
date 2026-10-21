# Status

## Milestone 2 (in progress): shipping and GST master foundation

**Checkout and live payments remain disabled.** Tax is NOT calculated yet: the GST master
is administration-only and not used by checkout. Payments are still USD-only.

### Phase 2.1 shipping (done)
- Free-shipping threshold now requires `FREE_SHIPPING_THRESHOLD_BASIS` (only `subtotal`
  is supported); the app refuses to start with a threshold and no basis.
- Every order stores `shipping_snapshot` (JSONB: charge, threshold, basis, amount applied,
  `tax_treatment: "undetermined"`). Legacy orders keep NULL. Migration `d5f1b2c9a604`.
- Shipping charge/threshold values are NOT invented: unset => checkout returns 503.

### Phase 2.2 GST master foundation (done, not used by checkout)
- Tables (migration `e7a3c9b1d845`, empty, nothing seeded): `tax_classifications`
  (HSN/SAC), `tax_rules` (effective-dated components, treatment, source reference),
  `gst_registrations` (business registrations; selecting one is NOT implemented).
- Admin-only API `/api/v1/admin/tax/...`: create, list, edit (draft only), approve, retire;
  rules also `close`. Everything is audited. Errors carry a stable `code`.
- Rules: all start as draft; approval needs a source reference, an approved classification
  and no overlap (also enforced by a DB exclusion constraint). Approved records are never
  edited; they are closed/retired and replaced.
- Approval: creator cannot approve their own record unless
  `TAX_MASTER_ALLOW_SELF_APPROVAL=true` (then a note is required and audited).
- Validation is format-only (HSN 2/4/6/8 digits, SAC 6 digits, GSTIN pattern + state prefix).
  Whether a code, rate or GSTIN is correct is for the GST professional.

### Immutability (database level)
Migration `f2b6d8a4c917` adds a trigger (`tax_master_guard`) on the three tax tables: records
must be inserted as drafts; an approved record can only be retired (an approved rule may
also have its end date brought forward, never extended); retired records are read-only;
non-draft records cannot be deleted; creator/approver can be cleared but not reassigned.
It protects against scripts and direct SQL, not against a superuser disabling triggers.
Test cleanup uses TRUNCATE, which does not fire row triggers.

### Known schema gap (NOT fixed here, needs its own migration)
`ck_orders_user_xor_guest` and `ck_carts_user_xor_guest` exist in the models but in no
migration, so a migrated database lacks them. CI reports them via
`scripts/check_schema_constraints.py` (allowlisted). Proposed fix, separate change:
1. Pre-check on the real database: `SELECT count(*) FROM orders WHERE NOT (...)` and the same
   for carts. If any rows violate it, stop and review them; do not rewrite data silently.
2. Migration adds both constraints as `NOT VALID` (instant, no table scan, no long lock),
   then `VALIDATE CONSTRAINT` in the same or a follow-up revision.
3. Remove the two names from `--allow` in CI.

### Phase 2.3 product tax assignment (done)
- `products.tax_classification_id` (nullable FK to an existing classification, RESTRICT) and
  `products.unit` (nullable, 2-8 upper-case letters, format only). Migration `b8d4f1a6c230`;
  existing products keep NULL = **unassigned**, which is never treated as tax-ready.
- `POST/PATCH /api/v1/products`: optional `tax_classification_id` and `unit`. Only an existing,
  **approved** classification can be assigned (422 `tax_classification_invalid` /
  `classification_not_approved`). PATCH: omit = unchanged, explicit `null` = un-assign. Changes
  are audited (`product.tax_assignment_changed`, from/to). Admin-only fields on product
  responses: `unit`, `tax_classification`, `tax_state` (`unassigned | assigned_unapproved |
  assigned_approved`); everyone else, and every cached public view, gets null.
- `GET /api/v1/admin/tax/products?state=...`: administrator worklist to find products to classify.
- Database triggers: a product can only be assigned an approved classification (row-locked, so a
  concurrent retirement cannot slip past), and a classification assigned to products cannot be
  retired. The service reports both with stable codes (`classification_in_use`).
- `TAX_CLASSIFICATION_REQUIRED` (default **false**: nothing changes). When true, activating a
  product and checking out both require an approved classification (422
  `product_tax_classification_required` / `product_tax_unassigned`), before any stock change.
  Turning it on makes existing unclassified active products unorderable: use the worklist first.
- `assigned_approved` only means a classification is assigned and approved. Whether a tax rule
  applies on a given date is decided by the calculator (Phase 2.4). No tax is calculated.
- Downgrading `b8d4f1a6c230` drops the two columns and therefore any assignments: back up first.
- Open: `unit` is not checked against an official list (a professional decision).

### Phase 2.4 tax-engine boundary (done; NO GST is calculated)
- `app/modules/tax/engine.py`: checkout asks a `TaxEngine` for tax. An engine returns a validated
  `TaxResult` (amount + audit snapshot) or **raises**; it can never return nothing or an implicit zero.
  Engines: `NotConfiguredTaxEngine` (default; checkout 503), `FlatInterimTaxEngine` (the pre-GST
  behaviour: one configured percentage on the merchandise subtotal; snapshot says
  `gst_calculated: false`), `GstTaxEngine` (placeholder: readiness checks, then 503
  `tax_engine_not_implemented`; it never returns a result).
- Selection: `TAX_CALCULATION_ENABLED=true` -> GST engine (takes precedence); else a configured
  `TAX_RATE_PERCENT` -> interim engine; else not configured. Default false/unset = checkout refuses.
- Readiness (read-only) per cart line: missing classification, unapproved classification, no approved
  rule effective on the date, ambiguous rule, unknown treatment -> 422 `tax_calculation_blocked`
  (product ids + reasons). The "as of" date is the India date (UTC+05:30); whether the order date is the
  right basis is a tax-professional decision.
- Tax is decided before any stock change; failures roll back with no order and no stock mutation.
- `orders.tax_snapshot` (JSONB, migration `c3d8a2e6f914`): engine, `gst_calculated`, rate, taxable value,
  tax, rounding, per-line classification ids. Legacy orders keep NULL. Order totals reconcile
  (total = subtotal + shipping + tax); shipping is not taxed by the interim engine.
- The engine module only reads the tax master; product and checkout requests have no write path to it
  (and approved rows are protected by database triggers).
- Payment safeguards added: `LIVE_PAYMENTS_ENABLED` (default false) refuses live Stripe keys
  (`sk_live_`/`rk_live_`) for creating payments and refunds (503 `live_payments_disabled`); test keys are
  unaffected. Late payments (order no longer pending) and payments for unknown intents are written to the
  audit log (`payment.late_unreconciled`, `payment.unmatched_success`), visible in `GET /audit-logs`.

### Payment-readiness blockers (live payments must stay disabled)
1. **Currency:** orders have no currency; payments are hard-coded `usd`, so an INR order would create a
   USD intent. Needs `orders.currency`, payment currency taken from the order, a minor-unit table and
   confirmation that the Stripe account supports INR. NOT fixed here (payment-currency phase).
2. **Late payments:** now visible in the audit log but nothing acts on them (no refund, no fulfilment
   policy). The stale-order job cancels orders without cancelling the Stripe intent, so a customer can pay
   an order that was just cancelled. Needs the reservation/expiry phase and a business policy.
3. **Admin status PATCH:** can still mark an order paid with no payment, refunded with no Stripe refund,
   and cancel without restocking. Proposed fix (admin-status phase): remove paid/refunded from PATCH, route
   cancellation through a service that restocks, refunds only via the refund endpoint.
4. **Webhook:** no amount/currency check against the order; unrecognised events are dropped; refunds or
   disputes made in the Stripe dashboard are not reconciled; refunds are two columns, not a table.
5. **Guests cannot pay** (payments require an account).
6. The interim tax engine is not a GST calculation; checkout totals are not tax-compliant until the GST engine.
7. Known schema gap: the two XOR constraints (see above).

### Phase 2.5 per-client GST configuration (done; GST still NOT calculated)
**Architecture / isolation.** The code has no tenant concept (no tenant/org columns or tables). The
verified model is **one deployment per client**: each client gets its own database (and Redis, via
`CACHE_KEY_PREFIX`, and Stripe keys). All tax data (registrations, classifications, rules, business
configuration) is therefore per-client by deployment isolation; nothing client-specific lives in code,
migrations or seeds (a test enforces: no GSTIN-like literals, no tax-data inserts in `app/`, `alembic/`,
`scripts/`). Never point two clients at one database. Sharing a database between clients would need
`tenant_id` on every table (a redesign): not done, not needed for deployment-per-client.
- `business_tax_settings` (migration `d6a9c4f1b873`, empty, nothing seeded): the business's approved
  configuration. Every choice is a required column, so nothing is defaulted: `pricing_mode`
  (inclusive | exclusive), `place_of_supply_basis` (delivery_state), `shipping_tax_treatment`
  (not_taxed | taxable_with_goods), `rounding_policy` (line_half_up | invoice_half_up), the GST
  `registration_id` it applies under, effective dates, and the professional's written approval
  (`source_reference`, required to approve). The allowed values are the modes the calculator will
  implement, not legal advice; the client's professional chooses.
- Same controls as the rest of the tax master: draft -> approved (distinct approver, or audited
  self-approval flag), approved records are immutable (service + DB trigger), at most one approved
  configuration per day (DB exclusion constraint), retire and replace. A registration used by a
  non-retired configuration cannot be retired.
- Admin API `/api/v1/admin/tax/settings` (create/list/edit draft/approve/retire) and
  `GET /api/v1/admin/tax/readiness`: a read-only report of exactly what still blocks GST activation
  (missing/ambiguous configuration, registration not approved/valid, active products unclassified or
  without an effective rule, calculator not implemented) plus payment blockers (currency not INR, orders
  have no currency). `ready` can only be true when `blockers` is empty; it cannot be today because
  `GST_CALCULATOR_IMPLEMENTED` is false.
- The GST engine now also blocks checkout (422 `tax_calculation_blocked`, `configuration (<reason>)`) when
  the configuration is missing, ambiguous, or its registration is not approved/valid on the date.
  A fully configured business is still refused (503 `tax_engine_not_implemented`).
- Limits: one pinned registration per configuration (multi-registration selection rules are not defined);
  only the registration's state is known as a location (no separate dispatch location); B2B, SEZ, exports,
  discounts and cess are unsupported.

### Client facts still needed before GST can be activated (per client)
1. GST registration status (regular / composition / unregistered). If unregistered or composition, GST
   may not be charged at all: the business must say so before anything is configured.
2. Each GSTIN, legal name, registration state and validity dates (entered through the admin API by the
   client, never by us). If more than one registration exists: which one applies to which supplies.
3. Dispatch/warehouse location(s) if different from the registration state.
4. Pricing mode: tax-inclusive or tax-exclusive. Existing prices are treated as exclusive today.
5. HSN/SAC for every product, from the client's GST professional.
6. For each classification: treatment and rates (CGST/SGST/UTGST/IGST), the notification reference and
   effective dates; cess if applicable.
7. Place of supply: confirmation that delivery-state applies to the client's supplies, and how to treat
   special cases (SEZ, exports, buyers with a GSTIN, union territories).
8. Shipping: charge, free threshold, serviceable areas, and the shipping tax treatment.
9. Rounding policy (per line or per invoice) and the CGST/SGST remainder rule.
10. Which date governs the rate (order, payment or supply).
11. Whether discounts exist (not supported yet).
12. Invoice numbering, e-invoicing applicability and credit-note policy (a later phase).
13. Payments: INR support on the client's Stripe account (currency work is a later phase).

### Phase 2.6 currency and money correctness (done; live payments still disabled)
**Model.** Money is an integer number of MINOR units of a currency (the `*_cents` / `*_paise` names are
historical). Decimal arithmetic wherever a fraction can appear; floats are rejected; rounding is explicit
half-up (`app/core/money.py`). Flat interim tax is computed on minor units, so it is correct for any
exponent.
- **Currency registry** (`app/core/money.py`): exponent per ISO 4217 and a `payment_verified` flag.
  Registered: usd, inr, eur, gbp (2 decimals), jpy (0), krw (0), kwd, bhd (3). **Verified for Stripe
  payments (against https://docs.stripe.com/currencies, checked 2026-10-18): usd, inr, eur, gbp, jpy only.**
  Stripe's page documents minor-unit integer amounts, two-decimal as the default and JPY as zero-decimal; it
  does NOT document three-decimal currencies or confirm KRW, so kwd/bhd/krw support arithmetic and orders
  but payments in them are refused (`payment_currency_not_verified`). Not claimed: any other currency.
- **Store currency** (per deployment = per client): table `store_settings`, set once by an administrator
  with `PUT /api/v1/store/currency`; public `GET /api/v1/store/currency` gives code and exponent.
  Checkout answers 503 `store_currency_not_configured` until it is set. **Locked once any order exists**
  (service 409 `store_currency_locked` + database trigger), so a setting can never reinterpret history.
  Existing deployments must set it after upgrading.
- **Orders** store `currency` (immutable once set, trigger); shipping and tax snapshots carry the currency
  and exponent. Orders created before this change keep currency NULL (unknown, never assumed) and cannot be
  paid until a decision is made about them.
- **Payments**: built from the ORDER's currency and total (no hard-coded USD). The currency must be verified
  AND enabled by the client in `PAYMENT_CURRENCIES_ENABLED` (empty = refused; Stripe account support is
  account-specific and cannot be verified by code). `to_stripe_amount` is the single conversion point (for
  every verified currency Stripe's amount is the minor-unit integer). A payment's amount/currency are
  immutable (trigger). An existing payment that disagrees with its order is never reused (422
  `payment_inconsistent`).
- **Webhooks** verify amount, `amount_received` and currency against our stored payment and order BEFORE any
  state change. Missing or different values never mark anything paid; they are recorded in the audit log
  (`payment.amount_mismatch`, `payment.unverifiable_success`, `payment.order_mismatch`).
- **Refunds** use the original payment's currency and amount; an order/payment currency disagreement is
  refused; a refund that differs from the request is recorded (`payment.refund_mismatch`).
- Readiness (`GET /api/v1/admin/tax/readiness`) payments section reports `store_currency_not_configured`,
  `payment_currency_not_verified`, `payment_currency_not_enabled`. INR is not special-cased.
- Not changed: shipping/tax setting names (`FLAT_SHIPPING_CENTS`, ...) remain minor units of the store
  currency; discounts do not exist; no currency conversion is done anywhere.

### Payment-readiness blockers after Phase 2.6 (live payments must stay disabled)
1. ~~Currency~~ addressed above, but each client must still enable their currency in
   `PAYMENT_CURRENCIES_ENABLED` after confirming their own Stripe account can charge it; minimum/maximum
   charge amounts per currency are not enforced (Stripe rejects out-of-range amounts).
2. Late payments are visible in the audit log but nothing acts on them; the stale-order job still does not
   cancel Stripe intents. (reservation/expiry phase + business policy)
3. Admin status PATCH can still mark paid/refunded without a payment/refund and cancel without restock.
4. Webhook: unrecognised events are dropped; dashboard refunds/disputes are not reconciled; refunds are two
   columns, not a table; Stripe calls run inside the DB transaction without an explicit timeout.
5. Guests cannot pay.
6. The interim tax engine is not a GST calculation.
7. Known schema gap: the two XOR constraints.
8. Legacy orders (currency NULL) need an explicit decision before they can be paid or refunded.

### Phase 2.7 order and payment lifecycle integrity (done; live payments still disabled)
**State transitions** (one service, `OrderLifecycleService`; the database re-enforces them with a trigger):
`pending -> paid | cancelled`, `paid -> fulfilled | refunded`, `fulfilled -> refunded`; `cancelled` and `refunded` are
final. `paid` needs a succeeded payment, `refunded` needs a provider-confirmed (succeeded) refund, `cancelled`
needs its stock released: the database refuses anything else, even raw SQL. History rows carry actor and `reason`.
- **Admin `PATCH /orders/{id}/status`** now accepts only `fulfilled` (from `paid`). `paid`, `refunded`, `cancelled`
  and `pending` return 409 `status_change_not_allowed` with the correct route. (API change.)
- **Cancel** `POST /orders/{id}/cancel` (owner or admin, unpaid orders only; paid orders: 409
  `cancellation_not_allowed`, they are refunded). Guests cannot cancel (no cookie-based cancel yet).

**Stock.** Reserved at checkout (decremented), released only by cancellation/expiry of an UNPAID order, exactly once
(`orders.stock_released_at`: set in the same transaction as the cancel; the trigger forbids repeating or skipping
it). A paid order's stock is never released by cancellation.

**Cancellation/expiry flow (no DB transaction is held while waiting on Stripe).** Phase 1: lock, decide, commit.
Phase 2: ask Stripe to cancel each open PaymentIntent. Phase 3: re-lock, re-check (a webhook may have won), then
cancel + restock together. Provider says `succeeded` -> not cancelled (409 `payment_in_progress`); provider
unreachable/timeout/unknown status -> NOT cancelled, stock stays reserved, a `cancel_uncertain` reconciliation
record is opened (409 `cancellation_unconfirmed`) and the expiry job is retried.
**Expiry.** The worker runs a scheduler: every `ORDER_EXPIRY_SWEEP_INTERVAL_SECONDS` (default 60; dedupe key per time
bucket so several workers enqueue once) it queues `sweep_stale_orders`, which queues one `expire_order` job per order
older than `PENDING_ORDER_TIMEOUT_MINUTES`. Jobs are durable, retried with backoff, and safe to run twice.
`worker` is now a service in both compose files. `scripts/cancel_stale_orders.py` only queues a sweep (the old direct
script/method that cancelled without asking Stripe was removed).
**Monitoring.** `GET /api/v1/admin/lifecycle/health`: stale orders, oldest pending age, last sweep, `sweep_overdue`,
open reconciliations, active refunds, failed jobs. Failed jobs stay visible in `background_jobs` (status `failed`).

**Late and unmatched payments.** The webhook locks the order row before deciding. A payment for an order that can no
longer be paid is recorded (payment `succeeded`, order untouched) in `payment_reconciliations` (once per problem,
redeliveries deduplicated) plus the audit log. NOTHING is refunded or fulfilled automatically. An administrator
lists `GET /admin/reconciliations`, then either `POST /admin/reconciliations/{id}/refund` (an explicit refund
request) or `.../resolve` with a written note. Unmatched payments, amount mismatches, unverifiable events and
dashboard-made refunds open records too.
**Refunds.** `POST /payments/{order_id}/refund` now REQUESTS a refund (202, status `requested`; asking again returns
the same refund; one active refund per payment by unique index). A job asks Stripe with an idempotency key (a retry
or a crash never creates a second refund). Only a provider-confirmed `succeeded` marks the refund, payment and then
the order refunded; `pending` waits for the provider's `refund.*` webhook; `failed` leaves the order paid and a new
refund can be requested; repeated unknown outcomes become `uncertain` (+ reconciliation) after
`REFUND_MAX_ATTEMPTS`. Full refunds only. (API change: was synchronous, 200.)
**Stripe calls** are bounded by `STRIPE_TIMEOUT_SECONDS`; a timeout means "unknown", never "failed". Creating a
payment no longer holds the order lock across Stripe: plan -> provider -> re-check; an intent created while the
order was being cancelled is cancelled again.

**Policies that need YOUR decision (safe defaults implemented):** (1) cancelling or self-service refunding a PAID
order (now: blocked, admin-requested refund only); (2) late payments (now: manual reconciliation, never automatic);
(3) partial refunds (now: unsupported); (4) the unpaid-order timeout (30 min) and whether expired orders email the
customer; (5) legacy orders with no currency (cannot be paid or refunded).

### Payment-readiness blockers after Phase 2.7 (live payments must stay disabled)
1. Each client must enable their currency in `PAYMENT_CURRENCIES_ENABLED` after confirming their Stripe account.
2. Webhook events other than payment_intent.succeeded/payment_failed and refund.* are ignored (e.g. disputes).
3. Guests cannot pay or cancel; the interim tax engine is not GST-compliant.
4. Known schema gap: the two XOR constraints.
5. Legacy orders (currency NULL) need an explicit decision.
6. Not exercised against real Stripe (test mode) in this environment: only fakes, including timeouts/races.

### Phase 2.8 webhook security, reconciliation, refunds/disputes, provider readiness (done; live payments disabled)
**Webhook security.** Signatures are verified by Stripe's own library over the **raw, unmodified bytes** with its 5-minute
replay tolerance (tests use real HMAC signatures through the real code: wrong secret, one changed byte, re-serialised
JSON, stale timestamp and garbage headers are all rejected). A missing header is 400; a payload over 1 MB is 413.
Secrets never appear in responses or logs (tested). **Bug fixed:** the Stripe SDK returns `StripeObject`s, which have no
`.get`, so the parsing added in Phases 2.6-2.7 would have crashed every real webhook with a 500; fakes hid it. Events are
now converted to plain dicts after verification.
**Event states** (`processed_webhook_events`, key `(provider, event_id)`; no payload stored): `processed`, `ignored`
(a type we do not act on: kept and listed at `GET /admin/webhook-events`, never silently dropped) and `failed`
(handling raised: its transaction rolls back so nothing persists, the failure and attempt count are recorded in a
separate transaction, and the provider's retry is recognised and processed). Duplicate and concurrent deliveries are
claimed atomically, so effects happen once. Recovery after a crash is the same path: nothing committed, the provider retries.
**Reconciliation.** Amount/currency are verified before any state change (Phase 2.6); unmatched, late, mismatched and
unverifiable events open reconciliation records; out-of-order events are safe (a failure after success never downgrades
a payment; a refund confirmation that beats our job means the job never asks the provider again).
**Refunds.** The cumulative limit is enforced **in the database** (the refunds of a payment, in flight or confirmed, can
never exceed its amount; the payment row is locked while checked). One refund started by NovaCart may be in flight per
payment. A request now refunds what is still refundable. Refunds made at the provider (e.g. its dashboard) are recorded
(`origin = provider`), keep `payments.refunded_amount_cents` true, and open an `external_refund` record; they never
change the order (business decision). A payment becomes `refunded` only when fully refunded; the order only after our own
provider-confirmed refund that completes it.
**Disputes** (`charge.dispute.*`): recorded in `disputes` with explicit audited transitions
(`open -> under_review -> won | lost | closed`; closed is final and enforced in the DB, so late/out-of-order events cannot
reopen it and are acknowledged with 200). A dispute opens a `dispute_opened` record (and `dispute_lost` when lost) for a
human; it changes NO order, stock or refund by itself. **Refunds are refused while a dispute is open** (422
`payment_disputed`). Listed at `GET /admin/disputes`.
**Provider readiness.** Every payment and event records its provider; webhook URLs are per provider
(`/payments/webhooks/{provider}`, unknown = 404); provider-specific verification/parsing stays inside the provider class.
Design for configurable providers (selection, credential storage, enable/disable, currencies, switching, rotation,
webhooks) is in `docs/PAYMENT_PROVIDERS.md`. **Recommended phase to build it:** its own milestone after the lifecycle
is verified on a real Stripe test account and the refund/late-payment policies are decided. Not built here.

**Known gaps / blockers (live payments must stay disabled):** only Stripe is implemented; `payments.provider_payment_id`
is still unique across providers (changes with the second provider); a payment event that arrives before its payment row
exists is recorded as unmatched, not retried; partial refunds requested through NovaCart remain unsupported (business
decision); disputes need a human response (no evidence submission); events other than payment, refund and dispute types
are recorded but not acted on; nothing exercised against real Stripe test mode in this environment (only real signature
verification and fakes for outbound calls); the two XOR constraints; legacy orders with no currency.

### Pre-2.9 review: INR default and the five payment-policy decisions (done; live payments disabled)
Status found BEFORE the changes, and what was done:
| Area | Found | Now |
|---|---|---|
| A. INR default | missing (unset store currency = checkout 503) | `DEFAULT_STORE_CURRENCY=inr`: a NEW store (no configured currency, no orders) uses it and it is persisted at the first order, so a later change of the default never alters the store. A store that already has orders but no configured currency gets NO default (503 until an admin chooses). Admin set/change existed (before the first order; locked after). The response now says `configured` and lists `warnings` (payments not verified / not enabled for that currency). Orders and payments already persist their own currency; nothing is converted. |
| 1. Paid-order cancellation | partial (ordinary cancel and admin PATCH correctly refuse paid orders; no dedicated workflow; paid stock never returned) | `POST /admin/orders/{id}/cancel-and-refund` (admin; PAID, not fulfilled): requests a full refund; ONLY after the provider confirms it does the order become `refunded` and its stock return, exactly once. A fulfilled order is refunded with the plain refund endpoint (no automatic restock). Fulfilling is blocked while the cancelling refund is in flight. Open dispute blocks it. |
| 2. Late payments | implemented (2.7/2.8) | unchanged; tested after EXPIRY too (reconciled, not reopened, not refunded, no second restock) |
| 3. Partial refunds | mostly (cumulative DB limit, external partials recorded, dispute block) but NovaCart refunded "the remainder" after an external partial, i.e. initiated a partial refund | NovaCart initiates FULL refunds only; if part was already refunded it refuses (422 `partial_refund_not_supported`); the rest is completed at the provider and recorded. |
| 4. Timeout | mostly (one setting, `PENDING_ORDER_TIMEOUT_MINUTES`, used by the sweep, the expiry job and the health report; no hard-coded copies) | the provider's per-intent cancel outcome is now recorded in the audit log; a payment that succeeds during cancel/expiry opens a `cancel_payment_succeeded` case (order not cancelled, stock kept, its webhook confirms it). |
| 5. Legacy currency | payment blocked; refund NOT blocked; no report/remediation | refunds and cancel-and-refund are blocked too (422 `order_currency_unknown`). `GET /admin/orders/unknown-currency` reports them (with their payments' currencies and why those are not proof). `POST /admin/orders/{id}/establish-currency` records the currency with typed evidence (provider record, accounting record, store history reviewed) + a reference, audited, one-time, immutable afterwards, cannot contradict a recorded payment, converts nothing. The migration assigns NO currency to any order. |
Not done / needs you: reporting by currency does not exist yet (nothing reads historical amounts across currencies);
how to decide each legacy order's currency is an operational task for your records; customer self-service cancel of a
paid order is not offered; the configurable provider system is still its own milestone.

### Confirmed decisions (enforced and tested)
**1. Paid-order cancellation is admin-only for the initial release.** A customer cannot cancel a paid order by any route:
the ordinary cancel refuses paid orders for EVERYONE (including administrators), and the cancel-and-refund, refund and
status endpoints require an administrator. An admin cancellation is `POST /admin/orders/{id}/cancel-and-refund`: it records
who asked (`refunds.requested_by_user_id` and the audit actor), requests the refund, and only a provider-confirmed refund
moves the order and returns stock (exactly once). **A future customer cancellation request must be a separate
request-and-review workflow** (a customer submits a request; an administrator reviews it and then runs the procedure above):
not built, and no endpoint lets a customer shortcut it.
**2. Legacy currency evidence** (`POST /admin/orders/{id}/establish-currency`, report at `GET /admin/orders/unknown-currency`).
Accepted evidence: provider record, accounting record, reviewed store history. Evidence must reliably establish the currency
for THAT order:
- a provider record must cite one of the order's own provider payment ids (an order with no payment has none to cite);
- an accounting record must cite the order's id or one of its payment ids;
- store history alone is accepted only when the reviewer attests it is unambiguous, it matches the store's current currency,
  and no change of the store currency is on record; otherwise it is insufficient;
- a payment already recorded for the order (immutable) must agree with the currency.
Every review is recorded (`order_currency_remediations`): reviewer, time, evidence type and reference, DECISION
(`established`, `unresolved_conflicting`, `unresolved_inconclusive`) and the remediation taken ("Order currency set to X" or
"Left unresolved; manual review required: why"), plus an audit entry. Evidence that conflicts or is inconclusive is recorded
and rejected: the currency stays unset, payments and refunds stay blocked, and the report shows `manual_review_required`.
A reviewer can also record "unresolved" explicitly (reason + note). A later review with proper evidence can still establish
it; only one `established` review per order, and a recorded currency never changes (database trigger). Nothing is converted.

### Next
Phase 2.9 (product assignment) then 2.4/2.5 need owner decisions: pricing mode, business
registrations, approved classifications/rates, place of supply, shipping tax, rounding.

## Milestone 1: critical checkout defects (complete, awaiting review)

**Checkout and live payments must stay disabled in production.** Shipping and tax are
unset by default, so checkout returns 503 until the owner configures them. GST
(Milestone 2) is not started.

### Done
- Prices are recomputed from locked product rows. A changed price returns `409` with
  `code: "price_changed"`, the old/new prices and the new subtotal; the cart is refreshed
  so the customer reviews, then checks out again.
- A shipping address is required (`address_id` xor `shipping_address`); empty bodies are 422.
  Saved-address checkout now works (the address repository is wired in).
- Shipping/tax are read from settings, fail closed when unset, and tax uses `Decimal`
  (half-up rounding). The old 500 / 8% defaults are removed.
- `Idempotency-Key` must be 1-255 chars. Reuse with a different request is `409`.
  Concurrent same-key requests are serialized (advisory lock) and return one order.
  Migration `c4e8a1d7f203` adds nullable `orders.request_hash`; older orders have NULL
  and replay as before.
- Refresh-token replay now persists the family revocation; concurrent refreshes with one
  token succeed at most once (row lock). Strict: a replay revokes the whole family.

### Verification (run, not assumed)
See the milestone report. Complete suite and lint were run once at the gate.

### Not done / risks
- No real shipping or tax values exist: owner and tax professional decision (Milestone 2).
- The frontend must handle 409 `price_changed` and 503, and single-flight token refresh.
- Still open from the audit: payments (guests cannot pay, late payments), admin status
  bypass, reservation/cleanup job, migration/model CHECK-constraint gap, deploy image.
