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

### Next
Phase 2.5 (product assignment) then 2.4/2.5 need owner decisions: pricing mode, business
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
