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

### Next
Phase 2.3 (product assignment) then 2.4/2.5 need owner decisions: pricing mode, business
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
