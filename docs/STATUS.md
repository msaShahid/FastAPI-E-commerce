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

### Next
Phase 2.7 (product assignment) then 2.4/2.5 need owner decisions: pricing mode, business
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
