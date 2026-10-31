"""GST tax invoices and credit notes, and the business profile printed on them."""

import uuid
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Cookie, Depends, Query, Response, status
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from app.core.database import DbSession
from app.modules.audit.dependencies.audit_deps import AuditServiceDep
from app.modules.auth.dependencies.auth import AdminUser, CurrentUser
from app.modules.cart.dependencies.cart_identity import GUEST_CART_COOKIE
from app.modules.invoices.exceptions.invoice_exceptions import InvoiceNotFoundError
from app.modules.invoices.models.business_profile import BusinessProfile
from app.modules.invoices.models.invoice import Invoice
from app.modules.invoices.schemas.invoice import (
    BusinessProfileRead,
    BusinessProfileWrite,
    InvoiceRead,
    InvoiceSummary,
)
from app.modules.invoices.services.invoice_service import get_profile, invoice_for, issue_invoice
from app.modules.invoices.services.render import render_invoice_html
from app.modules.orders.dependencies.order_deps import OrderServiceDep
from app.modules.orders.exceptions.order_exceptions import OrderNotFoundError
from app.shared.enums.roles import UserRole
from app.shared.pagination.schemas import PageParams, PaginatedResponse

invoices_router = APIRouter(tags=["invoices"])

_PRINT_HEADERS = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:", "X-Content-Type-Options": "nosniff",
                  "Cache-Control": "private, no-store"}


def _doc_type(kind: str) -> str:
    return "credit_note" if kind == "credit-note" else "invoice"


async def _document(db, order_id, kind: str) -> Invoice:
    document = await invoice_for(db, order_id, _doc_type(kind))
    if document is None:
        raise InvoiceNotFoundError()
    return document


# ---------------------------------------------------------------------------------------------- a customer's own documents
def _register_document_routes(path_name: str, label: str) -> None:
    """The four read routes of one document type, each with its own fixed path (no catch-all segment that could shadow other order routes)."""

    @invoices_router.get(f"/orders/{{order_id}}/{path_name}", response_model=InvoiceRead, name=f"get_order_{path_name}", description=f"The {label} of one of your orders (an administrator may read any).")
    async def read_document(order_id: uuid.UUID, user: CurrentUser, orders: OrderServiceDep, db: DbSession):
        order = await orders.get_order(order_id=order_id, user_id=user.id, is_admin=user.role == UserRole.ADMIN)
        return await _document(db, order.id, path_name)

    @invoices_router.get(f"/orders/{{order_id}}/{path_name}/print", response_class=HTMLResponse, name=f"print_order_{path_name}",
                         description=f"The {label} as a printable page (fetch it with your token, then open or print it).")
    async def print_document(order_id: uuid.UUID, user: CurrentUser, orders: OrderServiceDep, db: DbSession):
        order = await orders.get_order(order_id=order_id, user_id=user.id, is_admin=user.role == UserRole.ADMIN)
        document = await _document(db, order.id, path_name)
        return HTMLResponse(render_invoice_html(document.document), headers=_PRINT_HEADERS)

    @invoices_router.get(f"/orders/guest/{{order_id}}/{path_name}", response_model=InvoiceRead, name=f"get_guest_order_{path_name}")
    async def read_guest_document(
        order_id: uuid.UUID, orders: OrderServiceDep, db: DbSession,
        guest_cart_token: Annotated[str | None, Cookie(alias=GUEST_CART_COOKIE)] = None,
    ):
        if not guest_cart_token:
            raise OrderNotFoundError(order_id)
        order = await orders.get_guest_order(order_id=order_id, guest_token=guest_cart_token)
        return await _document(db, order.id, path_name)

    @invoices_router.get(f"/orders/guest/{{order_id}}/{path_name}/print", response_class=HTMLResponse, name=f"print_guest_order_{path_name}")
    async def print_guest_document(
        order_id: uuid.UUID, orders: OrderServiceDep, db: DbSession,
        guest_cart_token: Annotated[str | None, Cookie(alias=GUEST_CART_COOKIE)] = None,
    ):
        if not guest_cart_token:
            raise OrderNotFoundError(order_id)
        order = await orders.get_guest_order(order_id=order_id, guest_token=guest_cart_token)
        document = await _document(db, order.id, path_name)
        return HTMLResponse(render_invoice_html(document.document), headers=_PRINT_HEADERS)


_register_document_routes("invoice", "tax invoice")
_register_document_routes("credit-note", "credit note")


# ---------------------------------------------------------------------------------------------- administrators
@invoices_router.post("/orders/{order_id}/invoice", response_model=InvoiceRead, status_code=status.HTTP_201_CREATED)
async def issue_order_invoice(order_id: uuid.UUID, admin: AdminUser, orders: OrderServiceDep, db: DbSession, audit: AuditServiceDep, response: Response):
    """Issue the tax invoice of a paid or shipped GST order that has none (for example an order shipped before the business profile
    was set). Idempotent: an order that already has one returns it (200). A GST order is invoiced automatically when it is shipped."""
    order = await orders.get_order(order_id=order_id, user_id=admin.id, is_admin=True)
    existing = await invoice_for(db, order.id, "invoice")
    invoice = await issue_invoice(db, order, explicit=True)
    if existing is not None:
        response.status_code = status.HTTP_200_OK
    else:
        await audit.log(actor_user_id=admin.id, action="invoice.issued", target_type="order", target_id=order.id, details={"number": invoice.number})
    return invoice


@invoices_router.get("/admin/invoices", response_model=PaginatedResponse[InvoiceSummary])
async def list_invoices(
    _admin: AdminUser, db: DbSession, params: PageParams = Depends(),
    doc_type: Annotated[Literal["invoice", "credit_note"] | None, Query()] = None,
    from_date: date | None = None, to_date: date | None = None, order_id: uuid.UUID | None = None, number: str | None = None,
    fiscal_year: Annotated[str | None, Query(pattern=r"^\d{4}-\d{2}$")] = None,
):
    """Every tax invoice and credit note, newest first, filterable by type, date (issued_on, inclusive), order, number or financial year."""
    conditions = []
    if doc_type:
        conditions.append(Invoice.doc_type == doc_type)
    if from_date:
        conditions.append(Invoice.issued_on >= from_date)
    if to_date:
        conditions.append(Invoice.issued_on <= to_date)
    if order_id:
        conditions.append(Invoice.order_id == order_id)
    if number:
        conditions.append(Invoice.number == number)
    if fiscal_year:
        conditions.append(Invoice.fiscal_year == fiscal_year)
    total = (await db.execute(select(func.count()).select_from(Invoice).where(*conditions))).scalar_one()
    rows = (await db.execute(select(Invoice).where(*conditions).order_by(Invoice.issued_at.desc(), Invoice.number.desc()).offset(params.offset).limit(params.page_size))).scalars().all()
    return PaginatedResponse[InvoiceSummary](items=list(rows), total=total, page=params.page, page_size=params.page_size)


@invoices_router.get("/admin/business-profile", response_model=BusinessProfileRead | None)
async def read_business_profile(_admin: AdminUser, db: DbSession):
    """The seller details printed on invoices (null until set). Required before a GST order can be shipped."""
    return await get_profile(db)


@invoices_router.put("/admin/business-profile", response_model=BusinessProfileRead)
async def write_business_profile(payload: BusinessProfileWrite, admin: AdminUser, db: DbSession, audit: AuditServiceDep):
    """Set or replace the seller details. Invoices already issued keep the details they were issued with."""
    profile = await db.get(BusinessProfile, 1)
    values = payload.model_dump()
    if profile is None:
        profile = BusinessProfile(id=1, updated_by_user_id=admin.id, **values)
        db.add(profile)
    else:
        for key, value in values.items():
            setattr(profile, key, value)
        profile.updated_by_user_id = admin.id
    await db.flush()
    await db.refresh(profile)
    await audit.log(actor_user_id=admin.id, action="business_profile.updated", target_type="business_profile", target_id=1,
                    details={"trade_name": payload.trade_name, "state_code": payload.state_code, "invoice_prefix": payload.invoice_prefix})
    return profile
