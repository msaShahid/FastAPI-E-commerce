# Customer returns

Migration `b5e9c3a7d216` (additive: `return_requests` and `return_request_items`; no existing table or row is touched).

## The one rule that shapes everything about money

**NovaCart refunds a whole order at a time** (a payment-safety decision made earlier, enforced in the refund code: a second, partial refund
is refused with `PartialRefundNotSupportedError`). So returns are a **workflow around goods**, and money follows like this:

* A return that, together with the other received returns of the same order, brings back **every unit** of the order can **start that whole-order
  refund** (`POST /admin/returns/{id}/refund`). The existing refund machinery then does the rest (provider call, confirmation, order becomes `refunded`,
  and, for a GST order, a credit note).
* Any other return (some items of an order) **cannot** start a refund. The administrator refunds the customer **at the payment provider** (or sends
  a replacement, or decides on no refund) and closes the return with a recorded resolution (`refunded_outside`, `replacement_sent`, `no_refund`) and a
  note saying what was done. NovaCart does not move that money and does not issue a GST credit note for it.
* If you want NovaCart to refund PART of an order itself, that is a change to the payment code (provider partial-refund calls, reconciliation, disputes,
  credit notes for part of an order) that must be designed and tested against your real payment provider. It is a deliberate decision for you to make.

## Rules (this store's defaults)

| Rule | Behaviour |
|---|---|
| Who | A signed-in customer, for their own shipped order. Guests contact the store |
| When | The order is `fulfilled` (shipped) and within `RETURN_WINDOW_DAYS` (default 7) of **delivery** (the latest delivery date recorded on a shipment) or, if none is recorded, of dispatch. `0` switches returns off |
| What | Lines and quantities of the order. Across all requests that are not rejected or cancelled, a line can never be returned more than was bought; requests for one order are serialised on the order, so two simultaneous requests cannot both claim the last unit |
| Reasons | `damaged`, `defective`, `wrong_item`, `not_as_described`, `size_or_fit`, `changed_mind`, `other` (plus a comment of up to 1,000 characters, plain text) |
| Lifecycle | `requested` -> `approved` or `rejected` (a reason is shown to the customer) -> `received` -> `closed`. The customer can withdraw (`cancelled`) until the goods are received. An approved return that never arrives can be closed |
| Receiving | The administrator says, for EVERY line, how many units arrived and whether they go back in stock (only for goods in sellable condition). Restocking goes to the variant for a product with options, is recorded once per line and return, appears in the stock ledger (kind `restock`, with the order and the return) and can never exceed what was bought |
| Visibility | Customers see their returns, the decision, the rejection reason, the resolution and the refund's status. Administrators' notes, condition notes and the resolution note are internal |
| Audit | Every step is audited (`return.requested`, `approved`, `rejected`, `received`, `refund_requested`, `closed`, `cancelled`) |

## Endpoints

Customer: `GET /orders/{id}/returns/eligibility` (returnable quantities and the reason when not eligible), `POST /orders/{id}/returns`,
`GET /orders/{id}/returns`, `GET /returns`, `GET /returns/{id}`, `POST /returns/{id}/cancel`.

Administrator: `GET /admin/returns` (filters: `status`, `order_id`, `from_date`, `to_date`), `GET /admin/returns/{id}`, `POST .../approve`, `.../reject`,
`.../receive`, `.../refund`, `.../close`.

## Not built

Returns by guests, return shipping labels or pick-ups, restocking fees, exchanges (a replacement is sent by hand and recorded), store credit,
emails to the customer about a decision, a customer-visible return-by date per item, and refunds of part of an order.
