from datetime import UTC, datetime, timedelta

import pytest

from app.modules.categories.models.category import Category
from app.modules.products.models.product import Product


class FakeProductRepository:
    def __init__(self) -> None:
        self.products: dict[int, Product] = {}
        self._next_id = 1

    async def get_by_id(self, product_id: int) -> Product | None:
        return self.products.get(product_id)

    async def get_by_sku(self, sku: str) -> Product | None:
        return next(
            (p for p in self.products.values() if p.sku == sku),
            None,
        )

    async def get_by_slug(self, slug: str) -> Product | None:
        return next(
            (p for p in self.products.values() if p.slug == slug),
            None,
        )

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
        results = list(self.products.values())

        if category_id is not None:
            results = [p for p in results if p.category_id == category_id]

        if search:
            results = [p for p in results if search.lower() in p.name.lower()]

        if min_price is not None:
            results = [p for p in results if p.price_cents >= min_price]

        if max_price is not None:
            results = [p for p in results if p.price_cents <= max_price]

        is_descending = sort.startswith("-")
        sort_key = sort.lstrip("-")

        results.sort(
            key=lambda p: getattr(p, sort_key),
            reverse=is_descending,
        )

        total = len(results)

        return results[offset : offset + limit], total

    async def create(self, **fields) -> Product:
        fake_created_at = datetime(
            2024,
            1,
            1,
            tzinfo=UTC,
        ) + timedelta(seconds=self._next_id)

        product = Product(
            id=self._next_id,
            created_at=fields.pop("created_at", fake_created_at),
            updated_at=fields.pop("updated_at", fake_created_at),
            **fields,
        )

        self.products[self._next_id] = product
        self._next_id += 1

        return product

    async def update(self, product: Product, **fields) -> Product:
        for key, value in fields.items():
            setattr(product, key, value)

        return product


class FakeCategoryRepositoryForProducts:
    def __init__(self) -> None:
        self.categories: dict[int, Category] = {
            1: Category(
                id=1,
                name="Electronics",
                slug="electronics",
                is_active=True,
            ),
            2: Category(
                id=2,
                name="Discontinued",
                slug="discontinued",
                is_active=False,
            ),
        }

    async def get_by_id(self, category_id: int) -> Category | None:
        return self.categories.get(category_id)


class FakeStorageService:
    pass


@pytest.fixture
def fake_product_repository() -> FakeProductRepository:
    return FakeProductRepository()


@pytest.fixture
def fake_category_repository_for_products() -> FakeCategoryRepositoryForProducts:
    return FakeCategoryRepositoryForProducts()


@pytest.fixture
def fake_storage_service() -> FakeStorageService:
    return FakeStorageService()
