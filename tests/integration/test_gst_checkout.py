# ruff: noqa: F811  (fixtures imported from other suites are re-declared as test parameters)
"""Checkout under the GST calculator: place of supply, CGST/SGST versus IGST, inclusive and exclusive prices, shipping, mixed rates,
exempt goods, and the refusals (unknown state, delivery outside India) that must leave nothing behind."""

import uuid

import pytest
from sqlalchemy import text
from test_tax_engine_boundary import (  # noqa: F401
    ADDR,
    T,
    admin2,
    category,
    classification,
    gst_on,
    order_count,
    product,
    stock,
)

from app.core.config import get_settings

API = "/api/v1"


async def configure(client, h1, h2, *, pricing="exclusive", shipping="not_taxed", rounding="line_half_up", state="27"):
    """An approved registration (in `state`) and business configuration."""
    reg = (await client.post(f"{T}/registrations", headers=h1, json={
        "gstin": f"{state}ABCDE1234F1Z5", "legal_name": "Demo Traders", "state_code": state, "valid_from": "2020-01-01"})).json()
    assert (await client.post(f"{T}/registrations/{reg['id']}/approve", headers=h2)).status_code == 200
    s = (await client.post(f"{T}/settings", headers=h1, json={
        "pricing_mode": pricing, "place_of_supply_basis": "delivery_state", "shipping_tax_treatment": shipping, "rounding_policy": rounding,
        "registration_id": reg["id"], "effective_from": "2020-01-01", "source_reference": "TEST-APPROVAL"})).json()
    assert (await client.post(f"{T}/settings/{s['id']}/approve", headers=h2)).status_code == 200


async def rule(client, h1, h2, cid, cgst, sgst, igst, treatment="taxable"):
    r = (await client.post(f"{T}/rules", headers=h1, json={
        "classification_id": cid, "treatment": treatment, "cgst_rate": cgst, "sgst_utgst_rate": sgst, "igst_rate": igst,
        "effective_from": "2020-01-01", "source_reference": "TEST-NOTIF"})).json()
    assert (await client.post(f"{T}/rules/{r['id']}/approve", headers=h2)).status_code == 200


async def buy(client, items, *, address=None, key=None):
    """items: [(product_id, quantity)]. A guest checkout."""
    client.cookies.clear()
    for pid, qty in items:
        assert (await client.post(f"{API}/cart/items", json={"product_id": pid, "quantity": qty})).status_code == 201
    return await client.post(f"{API}/orders/checkout", json={"guest_email": "g@example.com", "shipping_address": address or ADDR},
                             headers={"Idempotency-Key": key or uuid.uuid4().hex})


@pytest.fixture(autouse=True)
def _shipping(monkeypatch):
    monkeypatch.setattr(get_settings(), "flat_shipping_cents", 5000)
    monkeypatch.setattr(get_settings(), "free_shipping_threshold_cents", None)


async def eighteen(client, h1, h2, category, sku, price, code="1234"):
    cid = await classification(client, h1, h2, code=code)
    await rule(client, h1, h2, cid, "9", "9", "18")
    return await product(client, h1, category, sku, cid=cid, price=price)


# ============================================================================================== place of supply
async def test_a_sale_inside_the_sellers_state_charges_cgst_and_sgst(client, admin_headers, admin2, category, gst_on):
    await configure(client, admin_headers, admin2)
    p = await eighteen(client, admin_headers, admin2, category, "INTRA", 100000)
    r = await buy(client, [(p["id"], 1)], address={**ADDR, "state": "Maharashtra"})
    assert r.status_code == 201, r.text
    o = r.json()
    assert (o["subtotal_cents"], o["shipping_cents"], o["tax_cents"], o["total_cents"], o["tax_included_cents"]) == (100000, 5000, 18000, 123000, 0)
    snap = o["tax_snapshot"]
    assert snap["supply_type"] == "intra_state" and snap["place_of_supply"] == {"state_code": "27", "state_name": "Maharashtra", "basis": "delivery_state"}
    assert snap["seller"]["gstin"] == "27ABCDE1234F1Z5" and snap["totals"]["cgst_paise"] == snap["totals"]["sgst_paise"] == 9000 and snap["totals"]["igst_paise"] == 0
    line = snap["lines"][0]
    assert (line["hsn_code"], line["taxable_paise"], line["cgst_paise"], line["sgst_paise"], line["rates"]["cgst"]) == ("1234", 100000, 9000, 9000, "9.000")


async def test_a_sale_to_another_state_charges_igst(client, admin_headers, admin2, category, gst_on):
    await configure(client, admin_headers, admin2)
    p = await eighteen(client, admin_headers, admin2, category, "INTER", 100000)
    o = (await buy(client, [(p["id"], 1)], address={**ADDR, "state": "Karnataka"})).json()
    assert o["tax_cents"] == 18000 and o["total_cents"] == 123000
    snap = o["tax_snapshot"]
    assert snap["supply_type"] == "inter_state" and snap["place_of_supply"]["state_code"] == "29"
    assert (snap["totals"]["igst_paise"], snap["totals"]["cgst_paise"], snap["totals"]["sgst_paise"]) == (18000, 0, 0)


@pytest.mark.parametrize(("state", "supply"), [("MH", "intra_state"), ("maharashtra ", "intra_state"), ("27", "intra_state"), ("DL", "inter_state"),
                                               ("New Delhi", "inter_state"), ("Orissa", "inter_state")])
async def test_the_delivery_state_is_read_however_the_customer_wrote_it(client, admin_headers, admin2, category, gst_on, state, supply):
    await configure(client, admin_headers, admin2)
    p = await eighteen(client, admin_headers, admin2, category, "WRITE", 1000)
    r = await buy(client, [(p["id"], 1)], address={**ADDR, "state": state})
    assert r.status_code == 201 and r.json()["tax_snapshot"]["supply_type"] == supply


async def test_a_state_that_cannot_be_recognised_or_a_foreign_address_is_refused_and_changes_nothing(client, admin_headers, admin2, category, gst_on, db_session):
    await configure(client, admin_headers, admin2)
    p = await eighteen(client, admin_headers, admin2, category, "REFUSE", 1000)
    unknown = await buy(client, [(p["id"], 1)], address={**ADDR, "state": "Atlantis"})
    assert unknown.status_code == 422 and unknown.json()["code"] == "delivery_state_unknown" and "Atlantis" in unknown.json()["detail"]
    abroad = await buy(client, [(p["id"], 1)], address={**ADDR, "country": "US"})
    assert abroad.status_code == 422 and abroad.json()["code"] == "delivery_country_not_supported"
    assert await order_count(db_session) == 0 and await stock(client, p["id"]) == 10
    assert len((await client.get(f"{API}/cart")).json()["items"]) == 1                                   # the cart is untouched


async def test_the_list_of_states_for_the_address_form_is_public(client):
    states = (await client.get(f"{API}/store/states")).json()
    assert {"code": "27", "name": "Maharashtra"} in states and {"code": "07", "name": "Delhi"} in states and len(states) == 37


# ============================================================================================== pricing, shipping, rounding
async def test_inclusive_prices_add_nothing_and_report_the_gst_they_contain(client, admin_headers, admin2, category, gst_on):
    await configure(client, admin_headers, admin2, pricing="inclusive")
    p = await eighteen(client, admin_headers, admin2, category, "INCL", 118000)
    o = (await buy(client, [(p["id"], 1)])).json()
    assert (o["subtotal_cents"], o["tax_cents"], o["tax_included_cents"], o["total_cents"]) == (118000, 0, 18000, 123000)
    t = o["tax_snapshot"]["totals"]
    assert (t["taxable_paise"], t["cgst_paise"], t["sgst_paise"], t["tax_included_paise"], t["tax_added_paise"], t["round_off_paise"]) == (100000, 9000, 9000, 18000, 0, 0)
    got = (await client.get(f"{API}/orders/guest/{o['id']}")).json()
    assert got["tax_included_cents"] == 18000 and got["total_cents"] == 123000                           # a later read says the same


async def test_shipping_is_taxed_with_the_goods_only_when_the_business_configuration_says_so(client, admin_headers, admin2, category, gst_on):
    await configure(client, admin_headers, admin2, shipping="taxable_with_goods")
    p = await eighteen(client, admin_headers, admin2, category, "SHIPT", 100000)
    o = (await buy(client, [(p["id"], 1)])).json()
    assert o["tax_cents"] == 18900 and o["total_cents"] == 100000 + 5000 + 18900                          # 18% of 1,050.00
    line = o["tax_snapshot"]["lines"][0]
    assert (line["shipping_paise"], line["taxable_paise"], line["cgst_paise"]) == (5000, 105000, 9450)
    assert o["tax_snapshot"]["shipping"]["taxed"] is True


async def test_shipping_is_left_untaxed_when_the_configuration_says_not_taxed(client, admin_headers, admin2, category, gst_on):
    await configure(client, admin_headers, admin2, shipping="not_taxed")
    p = await eighteen(client, admin_headers, admin2, category, "SHIPN", 100000)
    o = (await buy(client, [(p["id"], 1)])).json()
    assert o["tax_cents"] == 18000 and o["tax_snapshot"]["shipping"] == {"paise": 5000, "taxed": False, "untaxed_paise": 5000}


async def test_each_line_is_taxed_at_its_own_rate_and_exempt_goods_carry_no_tax(client, admin_headers, admin2, category, gst_on):
    await configure(client, admin_headers, admin2)
    a = await eighteen(client, admin_headers, admin2, category, "R18", 100000, code="1111")
    c5 = await classification(client, admin_headers, admin2, code="2222")
    await rule(client, admin_headers, admin2, c5, "2.5", "2.5", "5")
    b = await product(client, admin_headers, category, "R5", cid=c5, price=100000)
    c0 = await classification(client, admin_headers, admin2, code="3333")
    await rule(client, admin_headers, admin2, c0, "0", "0", "0", treatment="exempt")
    e = await product(client, admin_headers, category, "R0", cid=c0, price=50000)
    o = (await buy(client, [(a["id"], 1), (b["id"], 1), (e["id"], 2)])).json()
    assert o["subtotal_cents"] == 300000 and o["tax_cents"] == 18000 + 5000
    by_hsn = {x["hsn_code"]: x for x in o["tax_snapshot"]["lines"]}
    assert by_hsn["1111"]["tax_paise"] == 18000 and by_hsn["2222"]["tax_paise"] == 5000
    assert by_hsn["3333"]["tax_paise"] == 0 and by_hsn["3333"]["treatment"] == "exempt" and by_hsn["3333"]["taxable_paise"] == 100000
    t = o["tax_snapshot"]["totals"]
    assert t["taxable_paise"] == 300000 and t["cgst_paise"] + t["sgst_paise"] == 23000 and t["tax_paise"] == 23000


@pytest.mark.parametrize(("rounding", "cgst_total"), [("line_half_up", 75), ("invoice_half_up", 76)])
async def test_the_rounding_policy_of_the_business_configuration_is_applied(client, admin_headers, admin2, category, gst_on, rounding, cgst_total):
    await configure(client, admin_headers, admin2, rounding=rounding)
    cid = await classification(client, admin_headers, admin2)
    await rule(client, admin_headers, admin2, cid, "2.5", "2.5", "5")
    items = [(await product(client, admin_headers, category, f"RND{i}", cid=cid, price=1010))["id"] for i in range(3)]
    o = (await buy(client, [(i, 1) for i in items])).json()
    t = o["tax_snapshot"]["totals"]
    assert t["cgst_paise"] == t["sgst_paise"] == cgst_total and o["tax_cents"] == 2 * cgst_total
    assert sum(x["cgst_paise"] for x in o["tax_snapshot"]["lines"]) == cgst_total                          # the lines add up to the total


async def test_the_tax_on_an_order_is_frozen_when_its_rule_is_later_retired(client, admin_headers, admin2, category, gst_on, db_session):
    await configure(client, admin_headers, admin2)
    p = await eighteen(client, admin_headers, admin2, category, "FROZEN", 100000)
    o = (await buy(client, [(p["id"], 1)])).json()
    with pytest.raises(Exception, match="an approved rule cannot be edited"):                           # an approved rule can never be altered
        await db_session.execute(text("UPDATE tax_rules SET cgst_rate = 1, sgst_utgst_rate = 1, igst_rate = 2"))
    await db_session.rollback()
    rule_id = (await client.get(f"{T}/rules", headers=admin_headers)).json()["items"][0]["id"]
    assert (await client.post(f"{T}/rules/{rule_id}/retire", headers=admin_headers)).status_code == 200   # it can only be retired
    again = (await client.get(f"{API}/orders/guest/{o['id']}")).json()
    assert again["tax_cents"] == 18000 and again["tax_snapshot"] == o["tax_snapshot"]                     # the order keeps what it was charged
    refused = await buy(client, [(p["id"], 1)])                                                           # and new sales are blocked until a rule exists
    assert refused.status_code == 422 and "no_applicable_rule" in refused.json()["detail"]


async def test_a_product_with_options_is_taxed_by_its_products_classification_on_the_variants_price(client, admin_headers, admin2, category, gst_on):
    from test_variant_admin import tee
    await configure(client, admin_headers, admin2)
    cid = await classification(client, admin_headers, admin2)
    await rule(client, admin_headers, admin2, cid, "9", "9", "18")
    detail = await tee(client, admin_headers, category, status="active", stock_each=5)
    await client.patch(f"{API}/products/{detail['id']}", headers=admin_headers, json={"tax_classification_id": cid})
    variant = next(v for v in detail["variants"] if v["sku"] == "T-M-BLK")               # its own price: Rs 9.00
    client.cookies.clear()
    await client.post(f"{API}/cart/items", json={"variant_id": variant["id"], "quantity": 2})
    o = (await client.post(f"{API}/orders/checkout", json={"guest_email": "g@example.com", "shipping_address": ADDR},
                           headers={"Idempotency-Key": uuid.uuid4().hex})).json()
    assert o["subtotal_cents"] == 1800 and o["tax_cents"] == 324 and o["tax_snapshot"]["lines"][0]["unit_price_paise"] == 900


async def test_the_public_store_settings_report_whether_checkout_is_possible_under_the_gst_calculator(client, admin_headers, admin2, gst_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "tax_rate_percent", None)                         # no flat rate: only the GST calculator could tax
    off = (await client.get(f"{API}/store/settings")).json()
    assert off["checkout_available"] is False and "GST is switched on but the business tax configuration is not approved and in force" in off["unavailable_reasons"]
    await configure(client, admin_headers, admin2)
    on = (await client.get(f"{API}/store/settings")).json()
    assert on["checkout_available"] is True and not any("tax" in r.lower() for r in on["unavailable_reasons"])
