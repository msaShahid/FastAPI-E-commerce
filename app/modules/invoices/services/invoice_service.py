"""Issuing GST tax invoices and credit notes.

An invoice is issued when goods are dispatched (the order becomes fulfilled) for an order whose tax was calculated by the GST engine.
Everything printed on it is frozen into the stored document from the order's own records (its tax snapshot, items, address) and the
business profile at that moment, so later changes to rates, products or the profile never alter an issued invoice.
Numbers are consecutive within a series (one per document type per financial year, April to March) and at most 16 characters:
  tax invoice   INV2627/000001     credit note   CN2627/000001
A full refund of an invoiced order is followed by a credit note for the same amounts. Credit notes for PART of an order are not issued
automatically."""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.invoices.exceptions.invoice_exceptions import (
    BusinessProfileIncompleteError,
    InvoiceNotApplicableError,
)
from app.modules.invoices.models.business_profile import BusinessProfile
from app.modules.invoices.models.invoice import Invoice, InvoiceSequence
from app.modules.invoices.services.words import amount_in_words
from app.modules.orders.models.order import Order
from app.modules.orders.models.order_item import OrderItem
from app.modules.tax.gst_states import BY_CODE, resolve_state

IST = timezone(timedelta(hours=5, minutes=30))
CREDIT_NOTE_PREFIX = "CN"


def india_today() -> date:
    return datetime.now(IST).date()


def fiscal_year(day: date) -> str:
    """The Indian financial year (1 April to 31 March) a date falls in: 2026-27."""
    start = day.year if day.month >= 4 else day.year - 1
    return f"{start}-{str(start + 1)[2:]}"


def format_number(prefix: str, fy: str, sequence: int) -> str:
    return f"{prefix}{fy[2:4]}{fy[5:7]}/{sequence:06d}"


def is_gst_order(order: Order) -> bool:
    snap = order.tax_snapshot or {}
    return snap.get("engine") == "gst" and snap.get("gst_calculated") is True


async def get_profile(db: AsyncSession) -> BusinessProfile | None:
    return await db.get(BusinessProfile, 1)


async def invoice_for(db: AsyncSession, order_id, doc_type: str) -> Invoice | None:
    return (await db.execute(select(Invoice).where(Invoice.order_id == order_id, Invoice.doc_type == doc_type))).scalar_one_or_none()


async def _next_sequence(db: AsyncSession, series: str) -> int:
    """Takes the next number of a series. The upsert locks the series row until this transaction ends, so concurrent issuers queue up
    and the numbers stay consecutive; if the transaction rolls back, the number is given back with it."""
    statement = (
        pg_insert(InvoiceSequence).values(series=series, last_value=1)
        .on_conflict_do_update(index_elements=[InvoiceSequence.series], set_={"last_value": InvoiceSequence.last_value + 1})
        .returning(InvoiceSequence.last_value)
    )
    return (await db.execute(statement)).scalar_one()


async def _recipient_email(db: AsyncSession, order: Order) -> str | None:
    if order.guest_email:
        return order.guest_email
    from app.modules.auth.models.user import User

    user = await db.get(User, order.user_id) if order.user_id else None
    return user.email if user else None


def _address(profile: BusinessProfile) -> dict:
    state = BY_CODE.get(profile.state_code)
    return {"line1": profile.address_line1, "line2": profile.address_line2, "city": profile.city,
            "state_code": profile.state_code, "state_name": state.name if state else None, "pincode": profile.pincode}


async def _build_document(db: AsyncSession, order: Order, profile: BusinessProfile, *, doc_type: str, number: str, issued_on: date, fy: str,
                          invoice: Invoice | None) -> dict:
    snap = order.tax_snapshot
    items = list((await db.execute(select(OrderItem).where(OrderItem.order_id == order.id).order_by(OrderItem.id))).scalars().all())
    lines = snap["lines"]
    if len(items) != len(lines):
        raise RuntimeError(f"order {order.id}: its items and its tax calculation do not match")
    rows = []
    for number_in_list, (item, tax) in enumerate(zip(items, lines, strict=True), start=1):
        if item.product_id != tax["product_id"] or item.quantity != tax["quantity"]:
            raise RuntimeError(f"order {order.id}: item {item.id} does not match its tax line")
        options = ((item.variant_snapshot or {}).get("options")) or []
        label = f" ({' / '.join(f'{o['name']}: {o['value']}' for o in options)})" if options else ""
        rows.append({
            "sr": number_in_list, "description": item.product_name_snapshot + label, "sku": item.sku_snapshot, "hsn": tax["hsn_code"],
            "treatment": tax["treatment"], "quantity": item.quantity, "unit_price_paise": item.price_cents_snapshot,
            "value_paise": tax["value_paise"], "discount_paise": tax.get("discount_paise", 0), "shipping_paise": tax["shipping_paise"], "taxable_paise": tax["taxable_paise"],
            "rates": tax["rates"], "cgst_paise": tax["cgst_paise"], "sgst_paise": tax["sgst_paise"], "igst_paise": tax["igst_paise"],
            "tax_paise": tax["tax_paise"], "round_off_paise": tax["round_off_paise"], "gross_paise": tax["gross_paise"],
        })
    place = snap["place_of_supply"]
    recipient_state = resolve_state(order.shipping_state)
    totals = snap["totals"]
    document = {
        "type": "tax_invoice" if doc_type == "invoice" else "credit_note",
        "number": number, "date": issued_on.isoformat(), "fiscal_year": fy, "currency": (order.currency or "inr").upper(),
        "order_id": str(order.id),
        "supplier": {"legal_name": snap["seller"]["legal_name"], "trade_name": profile.trade_name, "gstin": snap["seller"]["gstin"],
                     "address": _address(profile), "email": profile.email, "phone": profile.phone},
        "recipient": {"name": order.shipping_recipient_name, "email": await _recipient_email(db, order),
                      "address": {"line1": order.shipping_line1, "line2": order.shipping_line2, "city": order.shipping_city,
                                  "state": order.shipping_state, "state_code": recipient_state.code if recipient_state else None,
                                  "postal_code": order.shipping_postal_code, "country": order.shipping_country}},
        "place_of_supply": place, "supply_type": snap["supply_type"], "pricing_mode": snap["pricing_mode"],
        "rounding_policy": snap["rounding_policy"], "shipping_tax_treatment": snap["shipping_tax_treatment"],
        "lines": rows,
        "shipping": {"charged_paise": order.shipping_cents, "taxed": snap["shipping"]["taxed"], "untaxed_paise": snap["shipping"]["untaxed_paise"]},
        "totals": {**totals, "goods_paise": order.subtotal_cents, "discount_paise": order.discount_cents, "shipping_paise": order.shipping_cents,
                   "grand_total_paise": order.total_cents},
        "coupon": ({"code": order.coupon_code} if order.coupon_code else None),
        "amount_in_words": amount_in_words(order.total_cents, order.currency or "inr"),
        "footer": profile.footer,
        "reverse_charge": False,
    }
    if invoice is not None:
        document["against_invoice"] = {"number": invoice.number, "date": invoice.document["date"]}
        document["reason"] = "The order was refunded in full"
    return document


async def _issue(db: AsyncSession, order: Order, doc_type: str, related: Invoice | None) -> Invoice:
    profile = await get_profile(db)
    if profile is None:
        raise BusinessProfileIncompleteError()
    issued_on = india_today()
    fy = fiscal_year(issued_on)
    prefix = profile.invoice_prefix if doc_type == "invoice" else CREDIT_NOTE_PREFIX
    sequence = await _next_sequence(db, f"{'INV' if doc_type == 'invoice' else 'CN'}:{fy}")
    number = format_number(prefix, fy, sequence)
    document = await _build_document(db, order, profile, doc_type=doc_type, number=number, issued_on=issued_on, fy=fy, invoice=related)
    invoice = Invoice(order_id=order.id, doc_type=doc_type, number=number, fiscal_year=fy, sequence=sequence, issued_on=issued_on,
                      related_invoice_id=related.id if related else None, total_paise=order.total_cents,
                      currency=(order.currency or "inr").upper(), document=document)
    db.add(invoice)
    await db.flush()
    return invoice


async def issue_invoice(db: AsyncSession, order: Order, *, explicit: bool = False) -> Invoice | None:
    """The tax invoice of an order (the existing one if already issued).

    Called when an order is dispatched: an order not taxed by the GST engine has no tax invoice (returns None); a GST order with no business
    profile refuses (the goods must not ship without their invoice). With `explicit` (an administrator asking for it) a non-GST or
    unpaid order is refused with an explanation instead of silently returning None."""
    existing = await invoice_for(db, order.id, "invoice")
    if existing is not None:
        return existing
    if not is_gst_order(order):
        if explicit:
            raise InvoiceNotApplicableError("This order's tax was not calculated by the GST engine, so it has no tax invoice")
        return None
    if order.status.value not in ("paid", "fulfilled"):
        raise InvoiceNotApplicableError("Only a paid or shipped order can be invoiced")
    return await _issue(db, order, "invoice", None)


async def issue_credit_note(db: AsyncSession, order: Order) -> Invoice | None:
    """After a FULL refund: a credit note for the whole invoice. None when the order was never invoiced."""
    invoice = await invoice_for(db, order.id, "invoice")
    if invoice is None:
        return None
    existing = await invoice_for(db, order.id, "credit_note")
    if existing is not None:
        return existing
    return await _issue(db, order, "credit_note", invoice)
