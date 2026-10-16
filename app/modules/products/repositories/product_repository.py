from collections.abc import Iterable

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value

from app.modules.products.models.product import Product
from app.shared.enums.product_status import ProductStatus


class ProductRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_by_id(self, product_id: int) -> Product | None:
        result = await self.db.execute(
            select(Product)
            .where(Product.id == product_id)
            .options(selectinload(Product.category))
        )
        return result.scalar_one_or_none()

    async def get_by_sku(self, sku: str) -> Product | None:
        result = await self.db.execute(select(Product).where(Product.sku == sku))
        return result.scalar_one_or_none()

    async def get_by_slug(self, slug: str) -> Product | None:
        result = await self.db.execute(select(Product).where(Product.slug == slug))
        return result.scalar_one_or_none()

    _SORT_COLUMNS = {
        "created_at": Product.created_at,
        "price_cents": Product.price_cents,
        "name": Product.name,
    }

    async def list_paginated(
        self,
        *,
        offset: int,
        limit: int,
        category_id: int | None = None,
        search: str | None = None,
        min_price: int | None = None,
        max_price: int | None = None,
        sort: str = "-created_at",
        include_inactive: bool = False,
    ) -> tuple[list[Product], int]:

        conditions = []
        if category_id is not None:
            conditions.append(Product.category_id == category_id)
        if search:
            conditions.append(Product.name.ilike(f"%{search}%"))
        if min_price is not None:
            conditions.append(Product.price_cents >= min_price)
        if max_price is not None:
            conditions.append(Product.price_cents <= max_price)
        if not include_inactive:
            conditions.append(Product.status == ProductStatus.ACTIVE)

        count_query = select(func.count()).select_from(Product)
        for condition in conditions:
            count_query = count_query.where(condition)
        total = (await self.db.execute(count_query)).scalar_one()

        is_descending = sort.startswith("-")
        sort_key = sort.lstrip("-")
        sort_column = self._SORT_COLUMNS[sort_key]

        items_query = (
            select(Product)
            .options(selectinload(Product.category))
            .order_by(sort_column.desc() if is_descending else sort_column.asc())
            .offset(offset)
            .limit(limit)
        )
        for condition in conditions:
            items_query = items_query.where(condition)

        items = list((await self.db.execute(items_query)).scalars().all())

        return items, total

    async def create(self, **fields) -> Product:
        product = Product(**fields)
        self.db.add(product)
        await self.db.flush()
        # ProductRead requires `category`. A freshly-flushed instance has not
        # loaded it, and lazy-loading inside async serialization raises
        # MissingGreenlet (HTTP 500), so load it eagerly here -- same as
        # get_by_id does with selectinload.
        await self.db.refresh(product, attribute_names=["category"])
        return product

    async def update(self, product: Product, **fields) -> Product:
        for key, value in fields.items():
            setattr(product, key, value)
        await self.db.flush()
        return product

    async def get_by_id_for_update(self, product_id: int) -> Product | None:

        # populate_existing is essential here. Checkout loads the cart (and
        # each item's Product) BEFORE it takes this lock, so the Product is
        # already in the session's identity map with the stock value from
        # that earlier read. Without populate_existing, SQLAlchemy keeps that
        # stale value even though the locked row now holds a newer one, and
        # the "enough stock?" check passes for stock that was already sold.
        result = await self.db.execute(
            select(Product)
            .where(Product.id == product_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def get_many_for_update(self, product_ids: Iterable[int]) -> dict[int, Product]:
        """
        Locks several products with ONE query (instead of one per product) and
        returns them by id. Missing ids are simply absent from the result.

        ORDER BY id makes Postgres take the row locks in ascending id order, the
        same global order the one-at-a-time loop used, so two concurrent
        checkouts with overlapping carts still cannot deadlock.
        populate_existing: same reason as get_by_id_for_update.
        """
        ids = sorted(set(product_ids))
        if not ids:
            return {}
        result = await self.db.execute(
            select(Product)
            .where(Product.id.in_(ids))
            .order_by(Product.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return {product.id: product for product in result.scalars().all()}

    async def adjust_stock_many(
        self, products: dict[int, Product], deltas: dict[int, int]
    ) -> None:
        """
        Adds each delta (negative = sell, positive = restock) to that product's
        stock with ONE UPDATE, instead of one UPDATE per product. `products` are
        the already-locked rows from get_many_for_update; the caller has checked
        the stock. (The ORM sends one UPDATE per changed object, because it also
        reads back updated_at, so this is a single SQL statement instead.)
        """
        deltas = {product_id: delta for product_id, delta in deltas.items() if delta}
        if not deltas:
            return
        await self.db.execute(
            update(Product)
            .where(Product.id.in_(deltas))
            .values(
                stock=Product.stock + case(deltas, value=Product.id),
                updated_at=func.now(),
            )
            .execution_options(synchronize_session=False)
        )
        # Keep the loaded objects in step with the database without marking
        # them modified (which would send the same change again on flush).
        for product_id, delta in deltas.items():
            product = products[product_id]
            set_committed_value(product, "stock", product.stock + delta)

    async def decrement_stock(self, product: Product, quantity: int) -> Product:
        product.stock -= quantity
        await self.db.flush()
        return product

    async def increment_stock(self, product: Product, quantity: int) -> Product:
        """Restocks -- e.g. releasing stock held by an abandoned, never-paid order."""
        product.stock += quantity
        await self.db.flush()
        return product
