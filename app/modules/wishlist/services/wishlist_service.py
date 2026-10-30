"""A registered customer's wishlist: products saved for later. Adding and removing are idempotent. Only products that are on sale can be
added; a saved product that is later withdrawn simply stops appearing in the list (it comes back if the product does)."""

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.modules.auth.models.user import User
from app.modules.products.exceptions.product_exceptions import ProductNotFoundError
from app.modules.products.models.product import Product
from app.modules.reviews.exceptions.review_exceptions import WishlistFullError
from app.modules.wishlist.models.wishlist_item import WishlistItem
from app.modules.wishlist.schemas.wishlist import WishlistState
from app.shared.enums.product_status import ProductStatus

WISHLIST_LIMIT = 200


class WishlistService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _count(self, user: User) -> int:
        return (await self.db.execute(select(func.count()).select_from(WishlistItem).where(WishlistItem.user_id == user.id))).scalar_one()

    async def add(self, user: User, product_id: int) -> WishlistState:
        product = await self.db.get(Product, product_id)
        if product is None or product.status != ProductStatus.ACTIVE:
            raise ProductNotFoundError(product_id)
        already = (await self.db.execute(select(WishlistItem.id).where(WishlistItem.user_id == user.id, WishlistItem.product_id == product_id))).first()
        if already is None:
            if await self._count(user) >= WISHLIST_LIMIT:
                raise WishlistFullError(WISHLIST_LIMIT)
            await self.db.execute(
                pg_insert(WishlistItem).values(user_id=user.id, product_id=product_id).on_conflict_do_nothing(constraint="uq_wishlist_items_user_product")
            )
        return WishlistState(product_id=product_id, in_wishlist=True, count=await self._count(user))

    async def remove(self, user: User, product_id: int) -> WishlistState:
        await self.db.execute(delete(WishlistItem).where(WishlistItem.user_id == user.id, WishlistItem.product_id == product_id))
        return WishlistState(product_id=product_id, in_wishlist=False, count=await self._count(user))

    async def product_ids(self, user: User) -> list[int]:
        rows = await self.db.execute(
            select(WishlistItem.product_id).join(Product, Product.id == WishlistItem.product_id)
            .where(WishlistItem.user_id == user.id, Product.status == ProductStatus.ACTIVE).order_by(WishlistItem.created_at.desc(), WishlistItem.id.desc())
        )
        return list(rows.scalars().all())

    async def list_products(self, user: User, offset: int, limit: int) -> tuple[list[Product], int]:
        """The saved products that are on sale, most recently saved first."""
        where = (WishlistItem.user_id == user.id, Product.status == ProductStatus.ACTIVE)
        total = (await self.db.execute(select(func.count()).select_from(WishlistItem).join(Product, Product.id == WishlistItem.product_id).where(*where))).scalar_one()
        rows = await self.db.execute(
            select(Product).join(WishlistItem, WishlistItem.product_id == Product.id).where(*where)
            .options(selectinload(Product.category), joinedload(Product.tax_classification))
            .order_by(WishlistItem.created_at.desc(), WishlistItem.id.desc()).offset(offset).limit(limit)
        )
        return list(rows.scalars().all()), total
