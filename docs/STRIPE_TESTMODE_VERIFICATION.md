# Stripe TEST-mode verification (runbook)

**Status: NOT PERFORMED.** Phases 2.1-2.9 were verified with real Stripe signature verification and event parsing, and with fakes for
every outbound Stripe call. Nothing has been run against the Stripe API. Do this before trusting the payment flow; it uses TEST keys
only. Live payments stay disabled (`LIVE_PAYMENTS_ENABLED=false`).

## Prerequisites
- A Stripe account in **test mode**, and its test secret key `sk_test_...` in your **local** `.env` (never in chat, git or logs).
- `PAYMENT_CURRENCIES_ENABLED` including the currency you test (e.g. `inr`), and the store currency set.
- The Stripe CLI (https://stripe.com/docs/stripe-cli), logged in to the same TEST account.

## 1. Outbound API checks (automated)
```powershell
docker compose exec -e STRIPE_TESTMODE_VERIFY=yes api python -m scripts.stripe_testmode_check --currency inr --amount 5000
```
The script refuses anything except an `sk_test_` key, requires the explicit flag, and requires `LIVE_PAYMENTS_ENABLED=false`. It checks:
| Check | Expected |
|---|---|
| Create a PaymentIntent | Stripe holds exactly the amount (minor units) and currency the application computed |
| Same idempotency key twice | the same intent comes back |
| Cancel, twice | status `canceled` both times, never an error |
| Pay with the test card, then refund in full | payment succeeds for the exact amount; refund accepted in the original currency and amount |
| Same refund key twice | the same refund id (no double refund) |
| A second full refund | refused by Stripe |
| Impossibly short timeout | the explicit timeout error (bounded wait; the outcome is "unknown", never "failed") |

## 2. Webhooks with real Stripe test events (manual)
1. Forward events to your local API; the CLI prints a **test-mode** signing secret (`whsec_...`): put it in your local `.env` as `STRIPE_WEBHOOK_SECRET` and recreate the API container.
```powershell
stripe listen --forward-to http://localhost:8000/api/v1/payments/webhook
```
2. In another terminal, trigger events and check the result in NovaCart (admin endpoints below).
```powershell
stripe trigger payment_intent.succeeded
stripe trigger payment_intent.payment_failed
stripe trigger charge.dispute.created
```
Triggered events carry Stripe's own sample objects, so their payment ids will not match a NovaCart payment: **the expected NovaCart result is
that they are verified, accepted (HTTP 200) and recorded as unmatched/deferred, never applied to an order.** Check:
- `GET /api/v1/admin/webhook-events` shows them (`processed`, or `ignored` for types NovaCart does not act on);
- `GET /api/v1/admin/webhook-events/deferred` and `GET /api/v1/admin/reconciliations` show the unmatched ones;
- no order changed status.
3. End to end with a REAL NovaCart payment: create an order, `POST /api/v1/payments`, then confirm that PaymentIntent with the test card (Stripe
Dashboard or `stripe payment_intents confirm <pi_id> --payment-method pm_card_visa`). Expect the order to become `paid` once the
`payment_intent.succeeded` webhook arrives, with `GET /api/v1/admin/webhook-events` showing it `processed`.
4. Delayed, duplicate and out-of-order: resend an event from the Stripe Dashboard (Developers > Events > Resend) and expect 200 with no second effect;
stop the API, pay, restart it and let Stripe retry: expect the order to become paid once.
5. Expiry: create an order, create its payment, do not pay, set `PENDING_ORDER_TIMEOUT_MINUTES=1`, run the worker, and confirm in the Stripe Dashboard
that the PaymentIntent is `canceled` and the order `cancelled`.
6. Race: pay and let the order expire at the same moment; expect either a paid order or a cancelled order plus a `late_payment` reconciliation record, never both.
7. Refund: `POST /api/v1/payments/{order_id}/refund` as an admin; expect `202`, then the refund `succeeded` and the order `refunded` only after the provider confirms it.

## Record your result
Write the date, the Stripe account (test), and PASS/FAIL per row above in your release notes. Until you have done this, the honest status of
Stripe integration is: **signature verification and parsing verified; outbound calls not verified against Stripe.**
