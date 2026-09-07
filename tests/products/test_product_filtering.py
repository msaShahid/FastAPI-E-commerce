import pytest

from app.modules.products.services.product_service import ProductService
from app.shared.enums.product_status import ProductStatus


@pytest.fixture
def product_service(
    fake_product_repository,
    fake_category_repository_for_products,
    fake_storage_service,
) -> ProductService:
    return ProductService(
        fake_product_repository,
        fake_category_repository_for_products,
        fake_storage_service,
    )


async def _seed_products(service):
    """Three products, deliberately varied in price/name/category for filter tests."""
    await service.create_product(
        name="Cheap Pen",
        description=None,
        price_cents=299,
        sku="PEN-001",
        stock=100,
        category_id=1,
        status=ProductStatus.ACTIVE,
    )
    await service.create_product(
        name="Mid Notebook",
        description=None,
        price_cents=1299,
        sku="NOTE-001",
        stock=50,
        category_id=1,
        status=ProductStatus.ACTIVE,
    )
    await service.create_product(
        name="Expensive Pen Set",
        description=None,
        price_cents=4999,
        sku="PEN-002",
        stock=10,
        category_id=1,
        status=ProductStatus.ACTIVE,
    )


async def test_filter_by_search_matches_substring_case_insensitive(product_service):
    await _seed_products(product_service)

    results, total = await product_service.list_products(
        offset=0, limit=20, search="pen"
    )

    assert total == 2
    assert {p.name for p in results} == {"Cheap Pen", "Expensive Pen Set"}


async def test_filter_by_min_price(product_service):
    await _seed_products(product_service)

    results, total = await product_service.list_products(
        offset=0, limit=20, min_price=1000
    )

    assert total == 2
    assert all(p.price_cents >= 1000 for p in results)


async def test_filter_by_max_price(product_service):
    await _seed_products(product_service)

    results, total = await product_service.list_products(
        offset=0, limit=20, max_price=1000
    )

    assert total == 1
    assert results[0].name == "Cheap Pen"


async def test_filter_by_price_range(product_service):
    await _seed_products(product_service)

    results, total = await product_service.list_products(
        offset=0, limit=20, min_price=500, max_price=2000
    )

    assert total == 1
    assert results[0].name == "Mid Notebook"


async def test_sort_by_price_ascending(product_service):
    await _seed_products(product_service)

    results, _ = await product_service.list_products(
        offset=0, limit=20, sort="price_cents"
    )

    prices = [p.price_cents for p in results]
    assert prices == sorted(prices)


async def test_sort_by_price_descending(product_service):
    await _seed_products(product_service)

    results, _ = await product_service.list_products(
        offset=0, limit=20, sort="-price_cents"
    )

    prices = [p.price_cents for p in results]
    assert prices == sorted(prices, reverse=True)


async def test_combined_filters(product_service):
    """
    The realistic case: multiple filters AND a sort, all at once --
    proves they compose correctly rather than only being tested in
    isolation.
    """
    await _seed_products(product_service)

    results, total = await product_service.list_products(
        offset=0, limit=20, search="pen", min_price=1000, sort="-price_cents"
    )

    assert total == 1
    assert results[0].name == "Expensive Pen Set"


async def test_pagination_still_works_with_filters(product_service):
    await _seed_products(product_service)

    page_1, total = await product_service.list_products(offset=0, limit=1, search="pen")
    page_2, _ = await product_service.list_products(offset=1, limit=1, search="pen")

    assert total == 2
    assert len(page_1) == 1
    assert len(page_2) == 1
    assert page_1[0].id != page_2[0].id
