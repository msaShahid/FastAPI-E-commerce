from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.products.models.product import Product


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

    # async def list_paginated(self, *, offset: int, limit: int) -> tuple[list[Product], int]:

    #     total_result = await self.db.execute(select(func.count()).select_from(Product))
    #     total = total_result.scalar_one()

    #     items_result = await self.db.execute(
    #         select(Product)
    #         .options(selectinload(Product.category))
    #         .order_by(Product.created_at.desc())
    #         .offset(offset)
    #         .limit(limit)
    #     )
    #     items = list(items_result.scalars().all())

    #     return items, total

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
        return product

    async def update(self, product: Product, **fields) -> Product:
        for key, value in fields.items():
            setattr(product, key, value)
        await self.db.flush()
        return product

    async def get_by_id_for_update(self, product_id: int) -> Product | None:

        result = await self.db.execute(
            select(Product).where(Product.id == product_id).with_for_update()
        )
        return result.scalar_one_or_none()

    async def decrement_stock(self, product: Product, quantity: int) -> Product:
        product.stock -= quantity
        await self.db.flush()
        return product
