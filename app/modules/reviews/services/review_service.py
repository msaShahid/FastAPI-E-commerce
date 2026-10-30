"""Product reviews. Rules: only registered customers who BOUGHT the product (a paid or fulfilled order containing it) can review it; one
review per customer per product; a customer can edit or delete their own (unless the store hid it); everyone sees only published ones;
an administrator can hide (with a reason) or delete any. Text is stored as plain text and never interpreted."""

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit.services.audit_service import AuditService
from app.modules.auth.models.user import User
from app.modules.orders.models.order import Order
from app.modules.orders.models.order_item import OrderItem
from app.modules.products.exceptions.product_exceptions import ProductNotFoundError
from app.modules.products.models.product import Product
from app.modules.reviews.exceptions.review_exceptions import (
    AlreadyReviewedError,
    InvalidReviewError,
    ReviewHiddenError,
    ReviewNotFoundError,
    ReviewRequiresPurchaseError,
)
from app.modules.reviews.models.product_review import ProductReview
from app.modules.reviews.schemas.reviews import (
    ReviewAdminRead,
    ReviewCreate,
    ReviewModeration,
    ReviewRead,
    ReviewSummary,
    ReviewUpdate,
)
from app.shared.enums.order_status import OrderStatus
from app.shared.enums.product_status import ProductStatus

SORTS = {"newest": (ProductReview.created_at.desc(), ProductReview.id.desc()), "highest": (ProductReview.rating.desc(), ProductReview.id.desc()),
         "lowest": (ProductReview.rating.asc(), ProductReview.id.desc())}


def display_name(username: str) -> str:
    """The first two characters of the username and asterisks for the rest ("pr*********"): a customer's chosen name, never their email."""
    return username[:2] + "*" * max(len(username) - 2, 1)


class ReviewService:
    def __init__(self, db: AsyncSession, audit: AuditService | None = None) -> None:
        self.db = db
        self.audit = audit

    # ------------------------------------------------------------------------------------------------ helpers
    async def _public_product(self, product_id: int) -> Product:
        product = await self.db.get(Product, product_id)
        if product is None or product.status != ProductStatus.ACTIVE:
            raise ProductNotFoundError(product_id)
        return product

    async def _bought(self, user_id, product_id: int) -> bool:
        found = await self.db.execute(
            select(OrderItem.id).join(Order, Order.id == OrderItem.order_id)
            .where(Order.user_id == user_id, OrderItem.product_id == product_id, Order.status.in_([OrderStatus.PAID, OrderStatus.FULFILLED]))
            .limit(1)
        )
        return found.first() is not None

    def _read(self, review: ProductReview, username: str, *, show_status: bool = False) -> ReviewRead:
        return ReviewRead(
            id=review.id, product_id=review.product_id, rating=review.rating, title=review.title, body=review.body,
            display_name=display_name(username), created_at=review.created_at, updated_at=review.updated_at,
            status=review.status if show_status else None,
        )

    async def _mine(self, user: User, product_id: int) -> ProductReview:
        review = (await self.db.execute(select(ProductReview).where(ProductReview.product_id == product_id, ProductReview.user_id == user.id))).scalar_one_or_none()
        if review is None:
            raise ReviewNotFoundError()
        return review

    # ------------------------------------------------------------------------------------------------ customers
    async def create(self, user: User, product_id: int, payload: ReviewCreate) -> ReviewRead:
        if await self.db.get(Product, product_id) is None:
            raise ProductNotFoundError(product_id)
        if not await self._bought(user.id, product_id):
            raise ReviewRequiresPurchaseError()
        review = ProductReview(product_id=product_id, user_id=user.id, rating=payload.rating, title=payload.title, body=payload.body)
        try:
            async with self.db.begin_nested():
                self.db.add(review)
                await self.db.flush()
        except IntegrityError as exc:  # the unique (product, customer) rule, also under two simultaneous requests
            raise AlreadyReviewedError() from exc
        return self._read(review, user.username, show_status=True)

    async def my_review(self, user: User, product_id: int) -> ReviewRead:
        return self._read(await self._mine(user, product_id), user.username, show_status=True)

    async def update_mine(self, user: User, product_id: int, payload: ReviewUpdate) -> ReviewRead:
        review = await self._mine(user, product_id)
        if review.status == "hidden":
            raise ReviewHiddenError()
        sent = payload.model_fields_set
        if "rating" in sent:
            if payload.rating is None:
                raise InvalidReviewError("a review needs a rating")
            review.rating = payload.rating
        if "title" in sent:
            review.title = payload.title
        if "body" in sent:
            review.body = payload.body
        await self.db.flush()
        await self.db.refresh(review)
        return self._read(review, user.username, show_status=True)

    async def delete_mine(self, user: User, product_id: int) -> None:
        review = await self._mine(user, product_id)
        await self.db.delete(review)
        await self.db.flush()

    # ------------------------------------------------------------------------------------------------ everyone
    async def list_public(self, product_id: int, sort: str, offset: int, limit: int):
        await self._public_product(product_id)
        where = (ProductReview.product_id == product_id, ProductReview.status == "published")
        total = (await self.db.execute(select(func.count()).select_from(ProductReview).where(*where))).scalar_one()
        rows = (await self.db.execute(
            select(ProductReview, User.username).join(User, User.id == ProductReview.user_id).where(*where).order_by(*SORTS[sort]).offset(offset).limit(limit)
        )).all()
        return total, [self._read(review, username) for review, username in rows]

    async def summaries(self, product_ids: list[int]) -> list[ReviewSummary]:
        """Published reviews only, for any number of products in ONE query."""
        ids = sorted(set(product_ids))
        stars = {i: {n: 0 for n in range(1, 6)} for i in ids}
        rows = (await self.db.execute(
            select(ProductReview.product_id, ProductReview.rating, func.count()).where(ProductReview.product_id.in_(ids), ProductReview.status == "published")
            .group_by(ProductReview.product_id, ProductReview.rating)
        )).all()
        for product_id, rating, count in rows:
            stars[product_id][rating] = count
        result = []
        for i in ids:
            total = sum(stars[i].values())
            average = round(sum(r * c for r, c in stars[i].items()) / total, 1) if total else None
            result.append(ReviewSummary(product_id=i, count=total, average=average, distribution=stars[i]))
        return result

    async def summary(self, product_id: int) -> ReviewSummary:
        await self._public_product(product_id)
        return (await self.summaries([product_id]))[0]

    # ------------------------------------------------------------------------------------------------ administrators
    async def admin_list(self, status: str | None, product_id: int | None, rating: int | None, offset: int, limit: int):
        where = []
        if status:
            where.append(ProductReview.status == status)
        if product_id is not None:
            where.append(ProductReview.product_id == product_id)
        if rating is not None:
            where.append(ProductReview.rating == rating)
        total = (await self.db.execute(select(func.count()).select_from(ProductReview).where(*where))).scalar_one()
        rows = (await self.db.execute(
            select(ProductReview, User.username).join(User, User.id == ProductReview.user_id).where(*where)
            .order_by(ProductReview.created_at.desc(), ProductReview.id.desc()).offset(offset).limit(limit)
        )).all()
        return total, [
            ReviewAdminRead(**self._read(review, username, show_status=True).model_dump(), user_id=str(review.user_id), hidden_reason=review.hidden_reason)
            for review, username in rows
        ]

    async def moderate(self, review_id: int, payload: ReviewModeration, admin: User) -> ReviewAdminRead:
        review = await self.db.get(ProductReview, review_id)
        if review is None:
            raise ReviewNotFoundError()
        if payload.status == "hidden":
            reason = (payload.reason or "").strip()
            if len(reason) < 3:
                raise InvalidReviewError("hiding a review needs a reason (at least 3 characters)")
            review.status, review.hidden_reason, review.hidden_by_user_id = "hidden", reason, admin.id
        else:
            review.status, review.hidden_reason, review.hidden_by_user_id = "published", None, None
        await self.db.flush()
        await self.db.refresh(review)
        if self.audit:
            await self.audit.log(actor_user_id=admin.id, action=f"review.{review.status}", target_type="review", target_id=review.id,
                                 details={"product_id": review.product_id, "reason": review.hidden_reason})
        username = (await self.db.get(User, review.user_id)).username
        return ReviewAdminRead(**self._read(review, username, show_status=True).model_dump(), user_id=str(review.user_id), hidden_reason=review.hidden_reason)

    async def admin_delete(self, review_id: int, admin: User) -> None:
        review = await self.db.get(ProductReview, review_id)
        if review is None:
            raise ReviewNotFoundError()
        details = {"product_id": review.product_id, "rating": review.rating}
        await self.db.execute(delete(ProductReview).where(ProductReview.id == review_id))
        if self.audit:
            await self.audit.log(actor_user_id=admin.id, action="review.deleted", target_type="review", target_id=review_id, details=details)


