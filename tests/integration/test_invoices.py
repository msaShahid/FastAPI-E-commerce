# ruff: noqa: F811  (fixtures imported from other suites are re-declared as test parameters)
"""GST tax invoices and credit notes: when they are issued, what they say, how they are numbered, who may read them, and that they never change."""

import asyncio
import uuid
from datetime import date

import pytest
from sqlalchemy import text
from test_cart_variants import add, signup
from test_gst_checkout import configure, eighteen, rule
from test_order_payment_lifecycle import hook, jobs, pay, provider  # noqa: F401
from test_tax_engine_boundary import ADDR, T, admin2, category, classification, gst_on, product  # noqa: F401
from test_variant_checkout import checkout

from app.core.config import get_settings

API = "/api/v1"
PROFILE = {"trade_name": "Demo Traders", "address_line1": "12 Market Road", "address_line2": "Fort", "city": "Mumbai", "state_code": "27",
           "pincode": "400001", "email": "sales@demo.example", "phone": "+91 22 5550 0000", "invoice_prefix": "INV", "footer": "Thank you for shopping."}
INVOICE_MODULE = "app.modules.invoices.services.invoice_service"


@pytest.fixture(autouse=True)
def _shipping(monkeypatch):
    monkeypatch.setattr(get_settings(), "flat_shipping_cents", 5000)
    monkeypatch.setattr(get_settings(), "free_shipping_threshold_cents", None)


async def set_profile(client, headers, **changes):
    r = await client.put(f"{API}/admin/business-profile", json={**PROFILE, **changes}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def paid_order(client, provider, product_id, *, qty=1, address=None):
    """A signed-in customer buys, pays and the provider confirms. Returns (customer headers, order)."""
    headers, _ = await signup(client)
    assert (await add(client, headers, product_id=product_id, quantity=qty)).status_code == 201
    order = (await checkout(client, headers, address=address or ADDR)).json()
    assert order["status"] == "pending", order
    await pay(client, headers, order["id"])
    assert (await hook(client, provider.last_pi)).status_code == 200
    return headers, order


async def fulfil(client, admin_headers, order_id):
    return await client.patch(f"{API}/orders/{order_id}/status", json={"status": "fulfilled"}, headers=admin_headers)


async def gst_shop(client, admin_headers, admin2, category, test_session_factory, *, price=100000, **settings):
    # GST invoices are for rupee stores. The test store is USD and a store's currency is fixed once set, so (before any order exists)
    # the setting is cleared the way test cleanup does it, and set again.
    async with test_session_factory() as session:
        await session.execute(text("TRUNCATE store_settings"))
        await session.commit()
    assert (await client.put(f"{API}/store/currency", json={"currency": "inr"}, headers=admin_headers)).status_code == 200
    await configure(client, admin_headers, admin2, **settings)
    return await eighteen(client, admin_headers, admin2, category, f"INV{uuid.uuid4().hex[:6]}", price)


def fy_now():
    from app.modules.invoices.services.invoice_service import fiscal_year, india_today
    return fiscal_year(india_today())


# ============================================================================================== the business profile
async def test_the_business_profile_is_set_by_administrators_validated_and_audited(client, admin_headers, db_session):
    customer, _ = await signup(client)
    assert (await client.get(f"{API}/admin/business-profile", headers=customer)).status_code == 403
    assert (await client.put(f"{API}/admin/business-profile", json=PROFILE, headers=customer)).status_code == 403
    assert (await client.get(f"{API}/admin/business-profile", headers=admin_headers)).json() is None                # not set yet
    for bad in ({"pincode": "4000"}, {"pincode": "40000A"}, {"state_code": "28"}, {"state_code": "7"}, {"invoice_prefix": "inv"},
                {"invoice_prefix": "TOOLONG"}, {"invoice_prefix": ""}, {"trade_name": ""}, {"extra": 1}):
        assert (await client.put(f"{API}/admin/business-profile", json={**PROFILE, **bad}, headers=admin_headers)).status_code == 422, bad
    saved = await set_profile(client, admin_headers)
    assert saved["trade_name"] == "Demo Traders" and saved["invoice_prefix"] == "INV"
    changed = await set_profile(client, admin_headers, trade_name="New Name", invoice_prefix="TAX")
    assert changed["trade_name"] == "New Name" and (await client.get(f"{API}/admin/business-profile", headers=admin_headers)).json()["invoice_prefix"] == "TAX"
    assert (await db_session.execute(text("SELECT count(*) FROM business_profile"))).scalar_one() == 1                # always one row
    actions = {r[0] for r in (await db_session.execute(text("SELECT action FROM audit_logs"))).all()}
    assert "business_profile.updated" in actions


# ============================================================================================== when an invoice is issued
async def test_a_gst_order_cannot_ship_without_the_business_profile_and_is_invoiced_when_it_does(client, admin_headers, admin2, category, provider, gst_on, test_session_factory, db_session):
    p = await gst_shop(client, admin_headers, admin2, category, test_session_factory)
    _customer, order = await paid_order(client, provider, p["id"])
    refused = await fulfil(client, admin_headers, order["id"])
    assert refused.status_code == 422 and refused.json()["code"] == "business_profile_incomplete"
    await db_session.rollback()
    assert (await db_session.execute(text("SELECT status FROM orders WHERE id = :o"), {"o": order["id"]})).scalar_one() == "paid"   # nothing changed
    assert (await db_session.execute(text("SELECT count(*) FROM invoices"))).scalar_one() == 0

    await set_profile(client, admin_headers)
    assert (await fulfil(client, admin_headers, order["id"])).status_code == 200
    invoice = (await client.get(f"{API}/orders/{order['id']}/invoice", headers=admin_headers)).json()
    assert invoice["number"] == f"INV{fy_now()[2:4]}{fy_now()[5:7]}/000001" and invoice["doc_type"] == "invoice"
    assert (await client.post(f"{API}/orders/{order['id']}/invoice", headers=admin_headers)).status_code == 200       # asking again issues nothing new
    assert (await db_session.execute(text("SELECT count(*) FROM invoices"))).scalar_one() == 1


async def test_an_order_not_taxed_by_the_gst_engine_ships_without_an_invoice_or_a_profile(client, admin_headers, category, provider, db_session):
    p = await product(client, admin_headers, category, "NOGST", price=1000)                           # the default (flat interim) engine
    _customer, order = await paid_order(client, provider, p["id"])
    assert (await fulfil(client, admin_headers, order["id"])).status_code == 200
    assert (await client.get(f"{API}/orders/{order['id']}/invoice", headers=admin_headers)).status_code == 404
    explicit = await client.post(f"{API}/orders/{order['id']}/invoice", headers=admin_headers)
    assert explicit.status_code == 422 and explicit.json()["code"] == "invoice_not_applicable"
    assert (await db_session.execute(text("SELECT count(*) FROM invoices"))).scalar_one() == 0


async def test_shipping_through_a_shipment_record_also_issues_the_invoice(client, admin_headers, admin2, category, provider, gst_on, test_session_factory):
    p = await gst_shop(client, admin_headers, admin2, category, test_session_factory)
    await set_profile(client, admin_headers)
    _customer, order = await paid_order(client, provider, p["id"])
    r = await client.post(f"{API}/orders/{order['id']}/shipments", json={"carrier_name": "BlueDart", "fulfill_order": True}, headers=admin_headers)
    assert r.status_code == 201, r.text
    assert (await client.get(f"{API}/orders/{order['id']}/invoice", headers=admin_headers)).status_code == 200


async def test_an_administrator_can_issue_an_invoice_early_and_asking_twice_returns_the_same_one(client, admin_headers, admin2, category, provider, gst_on, test_session_factory):
    p = await gst_shop(client, admin_headers, admin2, category, test_session_factory)
    _customer, order = await paid_order(client, provider, p["id"])
    no_profile = await client.post(f"{API}/orders/{order['id']}/invoice", headers=admin_headers)
    assert no_profile.status_code == 422 and no_profile.json()["code"] == "business_profile_incomplete"
    await set_profile(client, admin_headers)
    first = await client.post(f"{API}/orders/{order['id']}/invoice", headers=admin_headers)
    again = await client.post(f"{API}/orders/{order['id']}/invoice", headers=admin_headers)
    assert first.status_code == 201 and again.status_code == 200 and first.json()["number"] == again.json()["number"]
    assert (await fulfil(client, admin_headers, order["id"])).status_code == 200                                      # shipping later reuses it
    assert (await client.get(f"{API}/orders/{order['id']}/invoice", headers=admin_headers)).json()["number"] == first.json()["number"]
    customer, _ = await signup(client)
    assert (await client.post(f"{API}/orders/{order['id']}/invoice", headers=customer)).status_code == 403
    pending = (await checkout_pending(client, p["id"]))
    assert (await client.post(f"{API}/orders/{pending}/invoice", headers=admin_headers)).status_code == 422          # unpaid: not invoiceable


async def checkout_pending(client, product_id):
    headers, _ = await signup(client)
    await add(client, headers, product_id=product_id, quantity=1)
    return (await checkout(client, headers, address=ADDR)).json()["id"]


# ============================================================================================== what it says
async def test_the_invoice_states_everything_the_gst_rules_require_and_matches_the_order(client, admin_headers, admin2, category, provider, gst_on, test_session_factory):
    p = await gst_shop(client, admin_headers, admin2, category, test_session_factory, price=100000)
    await set_profile(client, admin_headers)
    customer, order = await paid_order(client, provider, p["id"], qty=2, address={**ADDR, "recipient_name": "Asha Rao", "state": "Karnataka", "city": "Bengaluru"})
    assert (await fulfil(client, admin_headers, order["id"])).status_code == 200
    invoice = (await client.get(f"{API}/orders/{order['id']}/invoice", headers=customer)).json()
    d = invoice["document"]
    assert invoice["total_paise"] == order["total_cents"] == d["totals"]["grand_total_paise"] and invoice["currency"] == "INR"
    assert d["type"] == "tax_invoice" and d["number"] == invoice["number"] and d["fiscal_year"] == fy_now() and d["order_id"] == order["id"]
    assert d["supplier"]["gstin"] == "27ABCDE1234F1Z5" and d["supplier"]["legal_name"] == "Demo Traders" and d["supplier"]["trade_name"] == "Demo Traders"
    assert d["supplier"]["address"] == {"line1": "12 Market Road", "line2": "Fort", "city": "Mumbai", "state_code": "27", "state_name": "Maharashtra", "pincode": "400001"}
    assert d["recipient"]["name"] == "Asha Rao" and d["recipient"]["address"]["state_code"] == "29" and d["recipient"]["email"]
    assert d["place_of_supply"]["state_code"] == "29" and d["supply_type"] == "inter_state"                                     # Maharashtra -> Karnataka
    line = d["lines"][0]
    assert (line["hsn"], line["quantity"], line["unit_price_paise"], line["taxable_paise"], line["igst_paise"], line["cgst_paise"], line["rates"]["igst"]) == ("1234", 2, 100000, 200000, 36000, 0, "18.000")
    assert d["totals"]["igst_paise"] == 36000 and d["totals"]["grand_total_paise"] == 200000 + 5000 + 36000
    assert d["shipping"]["charged_paise"] == 5000 and d["shipping"]["untaxed_paise"] == 5000 and d["reverse_charge"] is False
    assert d["amount_in_words"] == "Rupees Two Thousand Four Hundred Ten Only" and d["footer"] == "Thank you for shopping."


async def test_an_invoice_for_a_sale_inside_the_state_shows_cgst_and_sgst_and_inclusive_prices_show_the_round_off(client, admin_headers, admin2, category, provider, gst_on, test_session_factory):
    p = await gst_shop(client, admin_headers, admin2, category, test_session_factory, price=99900, pricing="inclusive")
    await set_profile(client, admin_headers)
    _customer, order = await paid_order(client, provider, p["id"])
    await fulfil(client, admin_headers, order["id"])
    d = (await client.get(f"{API}/orders/{order['id']}/invoice", headers=admin_headers)).json()["document"]
    assert d["supply_type"] == "intra_state" and d["pricing_mode"] == "inclusive"
    t = d["totals"]
    assert (t["taxable_paise"], t["cgst_paise"], t["sgst_paise"], t["igst_paise"], t["round_off_paise"], t["tax_included_paise"]) == (84661, 7619, 7619, 0, 1, 15238)
    assert t["taxable_paise"] + t["cgst_paise"] + t["sgst_paise"] + t["round_off_paise"] == 99900                              # the books balance
    assert t["grand_total_paise"] == 99900 + 5000 == order["total_cents"]


async def test_a_product_with_options_names_the_chosen_variant_on_the_invoice(client, admin_headers, admin2, category, provider, gst_on, test_session_factory):
    from test_variant_admin import tee
    await configure(client, admin_headers, admin2)
    await set_profile(client, admin_headers)
    cid = await classification(client, admin_headers, admin2)
    await rule(client, admin_headers, admin2, cid, "2.5", "2.5", "5")
    detail = await tee(client, admin_headers, category, status="active", stock_each=5)
    await client.patch(f"{API}/products/{detail['id']}", headers=admin_headers, json={"tax_classification_id": cid})
    variant = next(v for v in detail["variants"] if v["sku"] == "T-M-BLU")
    headers, _ = await signup(client)
    await add(client, headers, variant_id=variant["id"], quantity=1)
    order = (await checkout(client, headers, address=ADDR)).json()
    await pay(client, headers, order["id"])
    await hook(client, provider.last_pi)
    await fulfil(client, admin_headers, order["id"])
    line = (await client.get(f"{API}/orders/{order['id']}/invoice", headers=headers)).json()["document"]["lines"][0]
    assert line["sku"] == "T-M-BLU" and line["description"].endswith("(Size: M / Colour: Blue)")


# ============================================================================================== numbering and immutability
async def test_invoice_numbers_are_consecutive_even_when_orders_ship_at_the_same_moment(client, admin_headers, admin2, category, provider, gst_on, test_session_factory, db_session):
    p = await gst_shop(client, admin_headers, admin2, category, test_session_factory)
    await set_profile(client, admin_headers)
    orders = [(await paid_order(client, provider, p["id"]))[1] for _ in range(5)]
    results = await asyncio.gather(*[fulfil(client, admin_headers, o["id"]) for o in orders])
    assert [r.status_code for r in results] == [200] * 5
    await db_session.rollback()
    sequences = sorted(r[0] for r in (await db_session.execute(text("SELECT sequence FROM invoices WHERE doc_type = 'invoice'"))).all())
    assert sequences == [1, 2, 3, 4, 5]                                                                                       # no gap, no duplicate
    numbers = {r[0] for r in (await db_session.execute(text("SELECT number FROM invoices"))).all()}
    assert len(numbers) == 5 and all(len(n) <= 16 for n in numbers)


async def test_a_new_financial_year_starts_a_new_series_and_the_prefix_comes_from_the_profile(client, admin_headers, admin2, category, provider, gst_on, test_session_factory, monkeypatch):
    p = await gst_shop(client, admin_headers, admin2, category, test_session_factory)
    await set_profile(client, admin_headers, invoice_prefix="TAX")
    numbers = []
    for day in (date(2027, 3, 31), date(2027, 4, 1), date(2027, 4, 2)):
        monkeypatch.setattr(f"{INVOICE_MODULE}.india_today", lambda day=day: day)
        _customer, order = await paid_order(client, provider, p["id"])
        await fulfil(client, admin_headers, order["id"])
        numbers.append((await client.get(f"{API}/orders/{order['id']}/invoice", headers=admin_headers)).json()["number"])
    assert numbers == ["TAX2627/000001", "TAX2728/000001", "TAX2728/000002"]                                                   # March 31 vs April 1


async def test_an_issued_invoice_never_changes_not_even_when_the_profile_or_the_product_does(client, admin_headers, admin2, category, provider, gst_on, test_session_factory, db_session):
    p = await gst_shop(client, admin_headers, admin2, category, test_session_factory)
    await set_profile(client, admin_headers)
    _customer, order = await paid_order(client, provider, p["id"])
    await fulfil(client, admin_headers, order["id"])
    before = (await client.get(f"{API}/orders/{order['id']}/invoice", headers=admin_headers)).json()
    await set_profile(client, admin_headers, trade_name="Completely Different Ltd", address_line1="Elsewhere 9", invoice_prefix="ZZZ")
    await client.patch(f"{API}/products/{p['id']}", headers=admin_headers, json={"name": "Renamed Product", "price_cents": 1})
    assert (await client.get(f"{API}/orders/{order['id']}/invoice", headers=admin_headers)).json() == before
    for statement in ("UPDATE invoices SET total_paise = 1", "DELETE FROM invoices", "UPDATE invoices SET document = '{}'::jsonb"):
        with pytest.raises(Exception, match="cannot be changed or deleted"):
            await db_session.execute(text(statement))
        await db_session.rollback()


# ============================================================================================== who may read it, and the printable page
async def test_only_the_buyer_or_an_administrator_can_read_an_invoice(client, admin_headers, admin2, category, provider, gst_on, test_session_factory):
    p = await gst_shop(client, admin_headers, admin2, category, test_session_factory)
    await set_profile(client, admin_headers)
    owner, order = await paid_order(client, provider, p["id"])
    await fulfil(client, admin_headers, order["id"])
    url = f"{API}/orders/{order['id']}/invoice"
    stranger, _ = await signup(client)
    assert (await client.get(url, headers=owner)).status_code == 200 and (await client.get(url, headers=admin_headers)).status_code == 200
    assert (await client.get(url, headers=stranger)).status_code == 403
    client.cookies.clear()
    assert (await client.get(url)).status_code in (401, 403)
    assert (await client.get(f"{API}/orders/{uuid.uuid4()}/invoice", headers=owner)).status_code == 404
    assert (await client.get(f"{API}/orders/{order['id']}/credit-note", headers=owner)).status_code == 404                  # none was issued


async def test_a_guest_reads_their_invoice_with_their_cookie_and_nobody_else_can(client, admin_headers, admin2, category, provider, gst_on, test_session_factory):
    p = await gst_shop(client, admin_headers, admin2, category, test_session_factory)
    await set_profile(client, admin_headers)
    client.cookies.clear()
    await client.post(f"{API}/cart/items", json={"product_id": p["id"], "quantity": 1})
    order = (await client.post(f"{API}/orders/checkout", json={"guest_email": "guest@example.com", "shipping_address": ADDR}, headers={"Idempotency-Key": uuid.uuid4().hex})).json()
    assert (await client.post(f"{API}/payments", json={"order_id": order["id"]})).status_code == 201
    assert (await hook(client, provider.last_pi)).status_code == 200
    assert (await fulfil(client, admin_headers, order["id"])).status_code == 200
    mine = await client.get(f"{API}/orders/guest/{order['id']}/invoice")                                                       # the guest cookie is still in the jar
    assert mine.status_code == 200 and mine.json()["document"]["recipient"]["email"] == "guest@example.com"
    assert (await client.get(f"{API}/orders/guest/{order['id']}/invoice/print")).status_code == 200
    client.cookies.clear()
    assert (await client.get(f"{API}/orders/guest/{order['id']}/invoice")).status_code == 404                                  # no cookie, no invoice
    await client.post(f"{API}/cart/items", json={"product_id": p["id"], "quantity": 1})                                        # a different guest
    assert (await client.get(f"{API}/orders/guest/{order['id']}/invoice")).status_code == 404


async def test_the_printable_page_shows_the_invoice_and_treats_everything_in_it_as_text(client, admin_headers, admin2, category, provider, gst_on, test_session_factory):
    p = await gst_shop(client, admin_headers, admin2, category, test_session_factory)
    await client.patch(f"{API}/products/{p['id']}", headers=admin_headers, json={"name": "<script>alert(1)</script> Kurta"})
    await set_profile(client, admin_headers, footer="<b>Footer</b> & terms")
    customer, order = await paid_order(client, provider, p["id"], address={**ADDR, "recipient_name": "<img src=x onerror=alert(2)>"})
    await fulfil(client, admin_headers, order["id"])
    page = await client.get(f"{API}/orders/{order['id']}/invoice/print", headers=customer)
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    html = page.text
    assert "TAX INVOICE" in html.upper() and "27ABCDE1234F1Z5" in html and "Demo Traders" in html and "1,230.00" in html
    assert "<script>" not in html and "<img src=x" not in html and "&lt;script&gt;" in html and "&lt;b&gt;Footer&lt;/b&gt;" in html   # escaped, never interpreted
    assert "default-src 'none'" in page.headers["content-security-policy"] and page.headers["x-content-type-options"] == "nosniff"
    assert (await client.get(f"{API}/orders/{order['id']}/invoice/print", headers=(await signup(client))[0])).status_code == 403


# ============================================================================================== credit notes
async def test_a_full_refund_of_an_invoiced_order_is_followed_by_a_credit_note(client, admin_headers, admin2, category, provider, gst_on, test_session_factory, db_session):
    p = await gst_shop(client, admin_headers, admin2, category, test_session_factory)
    await set_profile(client, admin_headers)
    customer, order = await paid_order(client, provider, p["id"], qty=2)
    await fulfil(client, admin_headers, order["id"])
    invoice = (await client.get(f"{API}/orders/{order['id']}/invoice", headers=customer)).json()
    assert (await client.post(f"{API}/payments/{order['id']}/refund", headers=admin_headers)).status_code == 202
    await jobs(test_session_factory)
    note = (await client.get(f"{API}/orders/{order['id']}/credit-note", headers=customer)).json()
    assert note["doc_type"] == "credit_note" and note["number"] == f"CN{fy_now()[2:4]}{fy_now()[5:7]}/000001" and note["related_invoice_id"] == invoice["id"]
    assert note["total_paise"] == invoice["total_paise"] and note["document"]["totals"] == invoice["document"]["totals"]
    assert note["document"]["type"] == "credit_note" and note["document"]["against_invoice"] == {"number": invoice["number"], "date": invoice["document"]["date"]}
    html = (await client.get(f"{API}/orders/{order['id']}/credit-note/print", headers=customer)).text
    assert "CREDIT NOTE" in html.upper() and invoice["number"] in html
    await jobs(test_session_factory)                                                                                           # nothing is issued twice
    assert (await db_session.execute(text("SELECT count(*) FROM invoices"))).scalar_one() == 2
    listing = (await client.get(f"{API}/admin/invoices", params={"doc_type": "credit_note"}, headers=admin_headers)).json()
    assert listing["total"] == 1 and listing["items"][0]["number"] == note["number"]


async def test_cancelling_an_unshipped_paid_order_with_a_refund_needs_no_credit_note(client, admin_headers, admin2, category, provider, gst_on, test_session_factory, db_session):
    p = await gst_shop(client, admin_headers, admin2, category, test_session_factory)
    await set_profile(client, admin_headers)
    _customer, order = await paid_order(client, provider, p["id"])
    assert (await client.post(f"{API}/admin/orders/{order['id']}/cancel-and-refund", headers=admin_headers)).status_code == 202
    await jobs(test_session_factory)
    assert (await db_session.execute(text("SELECT count(*) FROM invoices"))).scalar_one() == 0                                   # it was never invoiced


# ============================================================================================== the administrator's list
async def test_the_invoice_list_is_paginated_and_filterable(client, admin_headers, admin2, category, provider, gst_on, test_session_factory):
    p = await gst_shop(client, admin_headers, admin2, category, test_session_factory)
    await set_profile(client, admin_headers)
    orders = []
    for _ in range(3):
        _customer, order = await paid_order(client, provider, p["id"])
        await fulfil(client, admin_headers, order["id"])
        orders.append(order["id"])
    customer, _ = await signup(client)
    assert (await client.get(f"{API}/admin/invoices", headers=customer)).status_code == 403
    everything = (await client.get(f"{API}/admin/invoices", headers=admin_headers)).json()
    assert everything["total"] == 3 and [i["number"][-6:] for i in everything["items"]] == ["000003", "000002", "000001"]       # newest first
    assert (await client.get(f"{API}/admin/invoices", params={"page_size": 2, "page": 2}, headers=admin_headers)).json()["items"][0]["number"].endswith("000001")
    assert (await client.get(f"{API}/admin/invoices", params={"order_id": orders[1]}, headers=admin_headers)).json()["total"] == 1
    assert (await client.get(f"{API}/admin/invoices", params={"number": everything["items"][0]["number"]}, headers=admin_headers)).json()["total"] == 1
    assert (await client.get(f"{API}/admin/invoices", params={"fiscal_year": "1999-00"}, headers=admin_headers)).json()["total"] == 0
    assert (await client.get(f"{API}/admin/invoices", params={"from_date": "2999-01-01"}, headers=admin_headers)).json()["total"] == 0
    assert (await client.get(f"{API}/admin/invoices", params={"fiscal_year": "bad"}, headers=admin_headers)).status_code == 422
