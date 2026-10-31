# GST: the calculator, tax invoices and credit notes

**This is software, not tax advice.** The calculator does exactly what an approved configuration tells it to, and every judgement call below
is a setting or an assumption written down so a tax professional can confirm or change it before real sales.

## What is switched on, and what is needed

The GST calculator is **off by default**. Until `TAX_CALCULATION_ENABLED=true`, checkout uses the interim flat rate (`TAX_RATE_PERCENT`).
When it is on, checkout calculates GST from **approved master data only**, and refuses (never taxes at zero) when anything is missing:

| Needed | Where it is set | If missing |
|---|---|---|
| An approved GST registration (GSTIN, legal name, state), valid today | `/admin/tax/registrations` | `422 tax_calculation_blocked` (`registration_not_approved`, ...) |
| An approved business tax configuration (the four decisions below) | `/admin/tax/settings` | `422 tax_calculation_blocked` (`no_approved_tax_settings`) |
| Every product has an approved HSN/SAC classification | product edit, `/admin/tax/classifications` | `422 tax_calculation_blocked` (`missing_classification`, naming the product) |
| One approved rule per classification, effective today | `/admin/tax/rules` | `422 tax_calculation_blocked` (`no_applicable_rule`) |
| A recognisable delivery state in India | the address | `422 delivery_state_unknown`, `422 delivery_country_not_supported` |
| Business profile (address, invoice prefix) to ship an order | `PUT /admin/business-profile` | `422 business_profile_incomplete` at dispatch |

Every record is created by one administrator and approved by a different one. `GET /admin/tax/readiness` lists exactly what is still missing.
The store currency must be INR for GST invoices (amounts are in paise).

## How the tax is worked out

1. **Place of supply** = the delivery state of the order (a sale to a consumer). The address's `state` is read tolerantly (name, old name, postal
   abbreviation such as `MH`, or the two-digit code); an unrecognised state is **refused**, never guessed. `GET /store/states` lists the valid
   states for the address form. Delivery outside India is refused.
2. **Within the seller's state** (delivery state code equals the registration's state code): **CGST + SGST/UTGST**. **Otherwise: IGST**
   ("Other Territory" counts as another state).
3. **Rates** come from the approved rule for each product's classification (effective on the checkout date in India time). A rule's treatment is
   `taxable`, `exempt`, `nil_rated` or `non_gst`; the last three carry no tax but still appear on the invoice with their taxable value.
4. The four decisions of the approved business configuration:
   * **pricing_mode** `exclusive`: listed prices are before GST and the tax is **added** to the total. `inclusive`: listed prices already contain
     GST; the customer pays the listed total and the invoice shows the GST inside it (`tax_cents` is 0, `tax_included_cents` carries it).
   * **shipping_tax_treatment** `not_taxed`: the shipping charge carries no GST. `taxable_with_goods`: it is taxed like the goods, **spread over the
     lines in proportion to their value, each part at its line's rate** (an assumption about mixed baskets that a professional should confirm).
   * **rounding_policy** `line_half_up`: tax rounded to the paisa on each line. `invoice_half_up`: rounded once per rate slab over the whole invoice
     and spread back to the lines. In both, lines add up to the invoice totals exactly.
   * **place_of_supply_basis** `delivery_state` (the only option).
5. In `inclusive` mode CGST and SGST are each computed from the taxable value, so they are **always equal**; the difference of a paisa or so between
   the listed price and taxable + tax is the invoice's explicit **round off**.
6. Everything used is frozen in the order's `tax_snapshot` (seller, place of supply, per-line HSN, rates and amounts, totals). Later changes to rules,
   products or the profile never change a placed order.

## Tax invoices and credit notes

* A GST order gets its **tax invoice when it is dispatched** (the order becomes `fulfilled`, by status change or by a shipment with `fulfill_order`).
  A GST order **cannot ship without it**: without a business profile dispatch is refused. Orders taxed by the interim flat rate have no tax invoice.
  An administrator may issue one earlier for a paid order: `POST /orders/{id}/invoice` (idempotent).
* **Numbers** are consecutive per series and financial year (April to March), at most 16 characters: `INV2627/000001` (the prefix is set in the
  profile, up to 5 characters), credit notes `CN2627/000001`. A number is taken inside the same database transaction that stores the invoice, so
  none is skipped or repeated, even when orders are dispatched at the same moment.
* An invoice shows the supplier (legal name and GSTIN from the approved registration, address from the profile), the recipient and delivery
  address, place of supply, each line's HSN, quantity, rate, taxable value, CGST/SGST or IGST, totals, round off, amount in words, and the date.
* An issued invoice is **frozen and immutable**: the database refuses any update or delete. A mistake is corrected by a credit note.
* A **full refund** of an invoiced order is followed automatically by a **credit note** for the same amounts, referring to the invoice.
* Reading: `GET /orders/{id}/invoice` and `/credit-note` (JSON), `.../print` (a printable page, fetch it with the token), guests under
  `/orders/guest/{id}/...` with their cookie; administrators: `GET /admin/invoices` (filters: type, dates, order, number, financial year).

## Not handled (decisions for you and your tax professional)

Sales to buyers who quote a GSTIN (B2B invoices), exports and SEZ supplies, reverse charge, the composition scheme, cess, tax collected at source
by an e-commerce operator, **e-invoicing (IRN/QR) and e-way bills** for businesses above the thresholds, HSN digit requirements by turnover,
discounts and coupons (not built yet), credit notes for part of an order, and services. Place of supply is the delivery state only.

## Trying it on the demo shop

`docker compose exec api python -m scripts.gst_demo_setup --classify-rest 3923` creates a **fictional** registration, one demo rate for every
HSN code, a configuration and a profile (the second administrator approves each, as in real use). Then set `TAX_CALCULATION_ENABLED=true`
and `TAX_CLASSIFICATION_REQUIRED=true` and restart. Replace everything with your own data before a real sale.
