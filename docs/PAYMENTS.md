# Payments: demo mode now, the client's Stripe or Razorpay later, all from the admin panel

Migration `c9a3e7d5f128` (additive). Nothing here needs a code change or a redeploy: the payment mode, the gateways, their credentials, their
testing and the switch to live payments are all admin-panel (admin API) operations. The credential encryption key `PROVIDER_SECRETS_KEY` must be
set once in the deployment's environment (it is the key, not a gateway credential).

## The three modes

| `payment_mode` | Who takes the payment | Real money? |
|---|---|---|
| `demo` | The built-in demo gateway (a simulator inside NovaCart) | **Never** |
| `test` | Stripe or Razorpay in their own sandbox, with the credentials an administrator entered | No |
| `live` | Stripe or Razorpay, real money | Yes, only after activation |
| `none` | Nobody: customers cannot pay (checkout still works up to payment) | n/a |

`GET /admin/payments/overview` shows the current mode, whether customers can pay right now, every gateway's connection status, whether live payments
can be activated and what still blocks it. The public `GET /store/settings` carries `payment_mode` and `payment_demo` so a storefront can show a clear
"DEMO MODE" or "TEST MODE" banner.

## Demo mode (works today, needs no accounts)

1. `POST /admin/payments/demo/enable {"confirm_no_real_money": true}`. It creates the demo gateway (no credentials: there is nothing to enter), enables it and
   makes it the store's gateway. Switch it off with `POST /admin/payments/demo/disable` (the store is then left with no gateway).
2. The storefront runs the normal flow: checkout, then `POST /payments {order_id}`. The payment is created with provider `demo` and `demo: true`.
3. The customer pays with **fake test details**, published by the public `GET /payments/demo/test-details` (so the storefront can show them): cards
   (`4242 4242 4242 4242` succeeds; `4000 0000 0000 0002` declined; `...9995` insufficient funds; `...0069` expired; `...0119` processing error), UPI ids
   (`success@demo`, `failure@demo`) and banks (`demo_bank_success`, `demo_bank_failure`). The storefront sends only the detail to
   `POST /payments/demo/pay {order_id, method, card_number | upi_id | bank}`.
4. **Real details are impossible to enter**: anything that is not on that published list is refused with `422 demo_only_test_details` before anything is
   looked up or recorded; the refusal never repeats what was typed; an expiry date, CVC, name or any other field is rejected; and only the outcome is
   kept (the demo gateway's database record holds an amount, a currency and a status, never a card number). The number is not logged.
5. A successful "payment" becomes a **signed event** delivered to the same webhook handling a live gateway's event goes through: the signature is verified
   against the demo gateway's own stored secret, the amount and currency are checked against the order, duplicates are ignored, and the order is marked
   paid by the normal lifecycle. A declined test card leaves the order unpaid and the customer can try again. A demo event can never mark a payment made
   with another gateway as paid. Refunds, cancellation and reconciliation work the same way.

### Demo transactions are marked everywhere

* A payment has `provider: "demo"` and `demo: true`; an order paid that way has `demo_payment: true` (set once, when the payment succeeds).
* **They are not sales**: sales reports exclude them (and show `demo_orders_excluded`), and they get **no tax invoice** and use no invoice number.
* The database refuses a demo configuration in any mode but `test`, and the demo gateway cannot be selected, or take a payment, while live payments are active.

## Switching to the client's real Stripe or Razorpay (when their credentials arrive)

All of this is in the admin panel, in this order:

1. **Enter the credentials**: `POST /admin/payment-providers {provider, mode: "test" | "live", credentials, webhook_secret?}`. Secrets are encrypted at
   rest, **write-only** (never in any response, log or error) and a key that does not match the mode (a `sk_live_` key in a test configuration) is refused.
   Start with the gateway's TEST credentials.
2. **Validate**: `POST .../{id}/validate` (a harmless authenticated call to the gateway) and set the currencies: `PUT .../{id}/currencies`.
3. **Test the gateway**: `POST .../{id}/check` runs the gateway check and stores its result: credentials accepted, the key is of the right mode, the store
   currency is enabled, a webhook secret is stored, a signed webhook event has arrived (point the gateway's dashboard at the URL shown), and, **in a test
   configuration**, a small test payment is created (and cancelled) in the gateway's sandbox. Nothing is ever charged, and **no payment is ever created on a
   live gateway**. Each check says `pass`, `fail`, `warn` or `skipped`, and whether it is required.
4. **Enable and select it** (`.../enable`, `.../prefer`): the store is now in `test` mode: a full sandbox rehearsal with the client's own accounts.
5. **For live**: repeat 1 to 3 with a `mode: "live"` configuration, then record the two attestations (`POST .../attestations`: that sandbox verification
   and a production review were done, with a reference). `.../enable` for a live configuration is refused until the webhook has delivered a signed event and both
   attestations exist.
6. **Activate**: `POST /admin/payments/live/activate {config_id, confirmation: "ACTIVATE LIVE PAYMENTS", note}`. It is refused, listing exactly why
   (`409 live_activation_blocked`), unless the live gateway is enabled with valid credentials, a verified webhook, both attestations and a **passing gateway check
   no older than 24 hours**, and the deployment's kill switch (`LIVE_PAYMENTS_ENABLED`, on by default) allows live. It then becomes the store's gateway.
7. **Deactivate** at any time: `POST /admin/payments/live/deactivate {reason}`. The store is left with **no gateway** (nothing, in particular not the demo gateway,
   takes over). Payments already made keep being reconciled and refunded; the configurations are kept.

Live payments therefore need credentials entered, validated, checked, attested, a verified webhook and an explicit activation, each recorded in the audit
log with who did it. A live Stripe key in the environment is always refused.

## Security and integrity, in short

* Credentials: encrypted with `PROVIDER_SECRETS_KEY`, write-only, never returned; only the last characters (a hint) are shown. Rotating them pauses new payments
  on that configuration until it is validated again.
* Webhooks: accepted only if the signature verifies with one of that provider's stored secrets (the URL alone authenticates nothing), within a time window,
  with the exact amount and currency of OUR stored payment; duplicates are processed once; anything unexpected is recorded for reconciliation, never treated as success.
* A payment is always continued, refunded and reconciled with the gateway configuration that created it, even after it is disabled or another is selected.
  There is never a silent switch from one gateway to another.

## What is and is not verified (be honest with the client)

* The demo gateway, the mode switches, activation and the gateway checks are tested end to end in this codebase.
* The **real** Stripe and Razorpay adapters have been exercised only against fakes and mocked HTTP: Stripe's webhook signature verification with real signatures,
  Razorpay with mocked responses. **No real call to Stripe or Razorpay has ever been made.** The sandbox rehearsal in steps 1 to 4 is exactly how that gets
  verified: do it with the client's test accounts before activating live, and read the check results.
* Razorpay supports INR only; Stripe supports the currencies listed in the catalog.
