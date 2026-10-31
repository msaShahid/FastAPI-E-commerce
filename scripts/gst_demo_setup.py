"""DEVELOPMENT ONLY. Fills in the GST master data the demo seed deliberately leaves empty, so the GST calculator and tax invoices can be
tried on the demo shop:

    docker compose exec api python -m scripts.gst_demo_setup [--state 27] [--rate 18] [--pricing inclusive|exclusive]

It creates, through the real admin API and with the second administrator approving (the approval rule is not bypassed):
  * an approved GST registration (a FICTIONAL GSTIN) in the chosen state,
  * one approved rule for every approved HSN code that has none, at ONE demo rate (CGST + SGST = half each, IGST = all of it),
  * an approved business tax configuration, and
  * the business profile printed on invoices (a fictional address).
THE RATES AND DETAILS ARE DEMO DATA, NOT TAX ADVICE. A real store enters its own registration, the rates its tax advisor confirms for each HSN
code, and its own configuration and address. Refuses in staging and production.

Then switch the calculator on (it is off by default) and restart the api and worker:
    TAX_CALCULATION_ENABLED=true   TAX_CLASSIFICATION_REQUIRED=true   (and the store currency must be INR, as in the demo seed)"""

import argparse
import asyncio
import sys

import httpx

from app.core.config import HARDENED_ENVIRONMENTS
from app.main import app
from scripts import seed_data as data
from scripts.seed import API, check, login, settings

DEMO_SOURCE = "DEMO DATA ONLY: not tax advice"
STATE_NAMES = {"27": "Maharashtra", "29": "Karnataka", "07": "Delhi", "33": "Tamil Nadu", "24": "Gujarat", "19": "West Bengal"}
ADDRESSES = {"27": ("12 Market Road, Fort", "Mumbai", "400001"), "29": ("12 Brigade Road", "Bengaluru", "560001"),
             "07": ("12 Connaught Place", "New Delhi", "110001"), "33": ("12 Anna Salai", "Chennai", "600002"),
             "24": ("12 Ashram Road", "Ahmedabad", "380009"), "19": ("12 Park Street", "Kolkata", "700016")}


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default="27", choices=sorted(STATE_NAMES), help="the seller's GST state code")
    parser.add_argument("--rate", default="18", help="the ONE demo GST rate (percent) for every taxable HSN code")
    parser.add_argument("--pricing", default="inclusive", choices=["inclusive", "exclusive"])
    parser.add_argument("--classify-rest", metavar="HSN", help="ALSO give every active product that has no tax classification this HSN code (the demo seed leaves "
                        "three unclassified on purpose, so that the readiness report has something to find). DEMO ONLY.")
    args = parser.parse_args()
    if settings.environment in HARDENED_ENVIRONMENTS:
        raise RuntimeError(f"Refusing to create demo GST data with ENVIRONMENT={settings.environment}.")
    rate = float(args.rate)
    half = f"{rate / 2:g}"
    T = f"{API}/admin/tax"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gst.local", timeout=60) as client:
        admin = await login(client, settings.seed_admin_email, settings.seed_admin_password)
        approver = await login(client, data.APPROVER_EMAIL, settings.seed_admin_password)
        if (await client.get(f"{T}/settings", headers=admin)).json().get("total"):
            print("A business tax configuration already exists: nothing to do.")
            return 0
        registration = check(await client.post(f"{T}/registrations", headers=admin, json={
            "gstin": f"{args.state}AAAAA0000A1Z5", "legal_name": "Demo Traders Private Limited", "state_code": args.state, "valid_from": "2017-07-01"}),
            (201,), "registration")
        check(await client.post(f"{T}/registrations/{registration['id']}/approve", headers=approver), (200,), "approve registration")
        classifications = check(await client.get(f"{T}/classifications", headers=admin, params={"status": "approved", "page_size": 100}), (200,), "classifications")["items"]
        existing = {r["classification_id"] for r in check(await client.get(f"{T}/rules", headers=admin, params={"status": "approved", "page_size": 100}), (200,), "rules")["items"]}
        made = 0
        for c in classifications:
            if c["id"] in existing:
                continue
            rule = check(await client.post(f"{T}/rules", headers=admin, json={
                "classification_id": c["id"], "treatment": "taxable", "cgst_rate": half, "sgst_utgst_rate": half, "igst_rate": f"{rate:g}",
                "effective_from": "2017-07-01", "source_reference": DEMO_SOURCE}), (201,), f"rule {c['code']}")
            check(await client.post(f"{T}/rules/{rule['id']}/approve", headers=approver), (200,), f"approve rule {c['code']}")
            made += 1
        cfg = check(await client.post(f"{T}/settings", headers=admin, json={
            "pricing_mode": args.pricing, "place_of_supply_basis": "delivery_state", "shipping_tax_treatment": "not_taxed", "rounding_policy": "line_half_up",
            "registration_id": registration["id"], "effective_from": "2017-07-01", "source_reference": DEMO_SOURCE}), (201,), "configuration")
        check(await client.post(f"{T}/settings/{cfg['id']}/approve", headers=approver), (200,), "approve configuration")
        line, city, pin = ADDRESSES[args.state]
        check(await client.put(f"{API}/admin/business-profile", headers=admin, json={
            "trade_name": "Demo Traders", "address_line1": line, "city": city, "state_code": args.state, "pincode": pin,
            "email": "sales@demo-traders.example", "phone": "+91 00000 00000", "invoice_prefix": "INV", "footer": "Demo data: not a real invoice."}), (200,), "profile")
        if args.classify_rest:
            target = next((c for c in classifications if c["code"] == args.classify_rest), None)
            if target is None:
                raise SystemExit(f"No approved HSN code {args.classify_rest!r}. Approved: " + ", ".join(sorted(c["code"] for c in classifications)))
            products = check(await client.get(f"{API}/products", headers=admin, params={"status": "active", "page_size": 100}), (200,), "products")["items"]
            for item in products:
                if item["status"] == "active" and item.get("tax_classification") is None:
                    check(await client.patch(f"{API}/products/{item['id']}", headers=admin, json={"tax_classification_id": target["id"]}), (200,), f"classify {item['sku']}")
                    print(f"  classified {item['sku']} as HSN {target['code']} (demo)")
        readiness = check(await client.get(f"{T}/readiness", headers=admin), (200,), "readiness")
    print(f"Demo GST data created: registration in {STATE_NAMES[args.state]} ({args.state}), {made} rule(s) at {args.rate}%, {args.pricing} prices, business profile.")
    print("Readiness:", "READY" if readiness["ready"] else "NOT READY: " + ", ".join(b["code"] for b in readiness["blockers"]))
    if not readiness["ready"]:
        print("(The demo seed leaves three active products without an HSN code on purpose. Re-run with --classify-rest <HSN>, or classify them in the admin.)")
    print("Now set TAX_CALCULATION_ENABLED=true and TAX_CLASSIFICATION_REQUIRED=true, make sure the store currency is INR, and restart the api and worker.")
    print("These are DEMO rates and details. Replace them with your tax advisor's before any real sale.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
