"""Read-only business reports for the administrator. Nothing here changes data. Every figure is defined in
`definitions` on the summary; money is in minor units of the store currency."""

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request

from app.core.database import DbSession
from app.core.rate_limit import limiter
from app.modules.auth.dependencies.auth import AdminUser
from app.modules.reports.repositories.report_repository import ReportRepository
from app.modules.reports.schemas.reports import LowStockItem, SalesByDay, Summary, TopProducts
from app.modules.reports.services.report_service import ReportService
from app.modules.store.repositories.store_repository import StoreRepository
from app.shared.pagination.schemas import PageParams, PaginatedResponse

reports_router = APIRouter(prefix="/admin/reports", tags=["reports"])


def get_report_service(db: DbSession) -> ReportService:
    return ReportService(ReportRepository(db), StoreRepository(db))


ServiceDep = Annotated[ReportService, Depends(get_report_service)]
Start = Annotated[date | None, Query(description="First day (inclusive), in the reporting timezone. Default: 29 days before `end`.")]
End = Annotated[date | None, Query(description="Last day (inclusive). Default: today. A report covers at most 366 days.")]


@reports_router.get("/summary", response_model=Summary)
@limiter.limit("30/minute")
async def summary(request: Request, _admin: AdminUser, service: ServiceDep, start: Start = None, end: End = None) -> Summary:
    """Headline numbers for a period: orders sold, units, gross, refunds, net, average order, orders by status, new customers."""
    return await service.summary(start, end)


@reports_router.get("/sales-by-day", response_model=SalesByDay)
@limiter.limit("30/minute")
async def sales_by_day(request: Request, _admin: AdminUser, service: ServiceDep, start: Start = None, end: End = None) -> SalesByDay:
    """One row per day (zero-filled), ready to chart."""
    return await service.sales_by_day(start, end)


@reports_router.get("/top-products", response_model=TopProducts)
@limiter.limit("30/minute")
async def top_products(
    request: Request, _admin: AdminUser, service: ServiceDep, start: Start = None, end: End = None,
    by: Literal["revenue", "units"] = "revenue", limit: Annotated[int, Query(ge=1, le=50)] = 10,
    group_by: Annotated[Literal["product", "variant"], Query(description="'variant' lists each size/colour separately")] = "product",
) -> TopProducts:
    return await service.top_products(start, end, by, limit, group_by)


@reports_router.get("/low-stock", response_model=PaginatedResponse[LowStockItem])
@limiter.limit("30/minute")
async def low_stock(
    request: Request, _admin: AdminUser, service: ServiceDep, params: PageParams = Depends(),
    threshold: Annotated[int, Query(ge=0, le=10000, description="Products with this many units or fewer")] = 10,
    include_inactive: Annotated[bool, Query(description="Also list draft and archived products")] = False,
) -> PaginatedResponse[LowStockItem]:
    """Products to restock, lowest first (out-of-stock included)."""
    total, items = await service.low_stock(threshold, include_inactive, params.offset, params.page_size)
    return PaginatedResponse[LowStockItem](items=items, total=total, page=params.page, page_size=params.page_size)
