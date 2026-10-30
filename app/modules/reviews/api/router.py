"""Product reviews. Public reading, customer writing (only for products they bought), administrator moderation."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.database import DbSession
from app.core.rate_limit import limiter
from app.modules.audit.dependencies.audit_deps import AuditServiceDep
from app.modules.auth.dependencies.auth import AdminUser, CurrentUser
from app.modules.reviews.schemas.reviews import (
    ReviewAdminRead,
    ReviewCreate,
    ReviewModeration,
    ReviewRead,
    ReviewSummary,
    ReviewUpdate,
)
from app.modules.reviews.services.review_service import ReviewService
from app.shared.pagination.schemas import PageParams, PaginatedResponse

reviews_router = APIRouter(prefix="/products", tags=["reviews"])
# (a separate prefix: "/products/anything" would be matched by GET /products/{product_id} first)
summaries_router = APIRouter(prefix="/reviews", tags=["reviews"])
admin_reviews_router = APIRouter(prefix="/admin/reviews", tags=["reviews"])


def get_review_service(db: DbSession, audit: AuditServiceDep) -> ReviewService:
    return ReviewService(db, audit)


ServiceDep = Annotated[ReviewService, Depends(get_review_service)]


# ---------------------------------------------------------------------------------------------- public
@summaries_router.get("/summaries", response_model=list[ReviewSummary])
async def review_summaries(service: ServiceDep, product_ids: Annotated[list[int], Query(min_length=1, max_length=50)]):
    """Rating average and distribution for up to 50 products at once (for the stars on a product list). Published reviews only."""
    return await service.summaries(product_ids)


@reviews_router.get("/{product_id}/reviews", response_model=PaginatedResponse[ReviewRead])
async def list_reviews(
    product_id: int, service: ServiceDep, params: PageParams = Depends(),
    sort: Literal["newest", "highest", "lowest"] = "newest",
):
    total, items = await service.list_public(product_id, sort, params.offset, params.page_size)
    return PaginatedResponse[ReviewRead](items=items, total=total, page=params.page, page_size=params.page_size)


@reviews_router.get("/{product_id}/review-summary", response_model=ReviewSummary)
async def review_summary(product_id: int, service: ServiceDep):
    return await service.summary(product_id)


# ---------------------------------------------------------------------------------------------- the signed-in customer
@reviews_router.post("/{product_id}/reviews", response_model=ReviewRead, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def create_review(request: Request, product_id: int, payload: ReviewCreate, user: CurrentUser, service: ServiceDep):
    """Review a product you bought (a paid or shipped order). One review per product; edit it afterwards."""
    return await service.create(user, product_id, payload)


@reviews_router.get("/{product_id}/reviews/mine", response_model=ReviewRead)
async def my_review(product_id: int, user: CurrentUser, service: ServiceDep):
    return await service.my_review(user, product_id)


@reviews_router.patch("/{product_id}/reviews/mine", response_model=ReviewRead)
@limiter.limit("10/minute")
async def update_my_review(request: Request, product_id: int, payload: ReviewUpdate, user: CurrentUser, service: ServiceDep):
    return await service.update_mine(user, product_id, payload)


@reviews_router.delete("/{product_id}/reviews/mine", status_code=status.HTTP_204_NO_CONTENT)
async def delete_my_review(product_id: int, user: CurrentUser, service: ServiceDep) -> None:
    await service.delete_mine(user, product_id)


# ---------------------------------------------------------------------------------------------- administrators
@admin_reviews_router.get("", response_model=PaginatedResponse[ReviewAdminRead])
async def admin_list_reviews(
    _admin: AdminUser, service: ServiceDep, params: PageParams = Depends(),
    status_filter: Annotated[Literal["published", "hidden"] | None, Query(alias="status")] = None,
    product_id: int | None = None, rating: Annotated[int | None, Query(ge=1, le=5)] = None,
):
    total, items = await service.admin_list(status_filter, product_id, rating, params.offset, params.page_size)
    return PaginatedResponse[ReviewAdminRead](items=items, total=total, page=params.page, page_size=params.page_size)


@admin_reviews_router.patch("/{review_id}", response_model=ReviewAdminRead)
async def moderate_review(review_id: int, payload: ReviewModeration, admin: AdminUser, service: ServiceDep):
    """Hide a review (a reason is required) or publish it again."""
    return await service.moderate(review_id, payload, admin)


@admin_reviews_router.delete("/{review_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_review(review_id: int, admin: AdminUser, service: ServiceDep) -> None:
    await service.admin_delete(review_id, admin)
