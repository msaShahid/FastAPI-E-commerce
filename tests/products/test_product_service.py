import pytest

from app.modules.products.exceptions.product_exceptions import (
    InvalidCategoryError,
    ProductNotFoundError,
)
from app.modules.products.services.product_service import ProductService
from app.shared.enums.product_status import ProductStatus


@pytest.fixture
def product_service(
    fake_product_repository, fake_category_repository_for_products, fake_storage_service
) -> ProductService:
    return ProductService(
        fake_product_repository,
        fake_category_repository_for_products,
        fake_storage_service,
    )


async def _make_product(service, **overrides):
    defaults = dict(
        name="Wireless Mouse",
        description=None,
        price_cents=1999,
        sku="MOUSE-001",
        stock=10,
        category_id=1,
        status=ProductStatus.ACTIVE,
    )
    defaults.update(overrides)
    return await service.create_product(**defaults)


async def test_create_product_with_valid_category_succeeds(product_service):
    product = await _make_product(product_service)
    assert product.category_id == 1
    assert product.slug == "wireless-mouse"


async def test_create_product_with_nonexistent_category_rejected(product_service):
    with pytest.raises(InvalidCategoryError):
        await _make_product(product_service, category_id=999, sku="X-001")


async def test_create_product_with_inactive_category_rejected(product_service):
    with pytest.raises(InvalidCategoryError):
        await _make_product(product_service, category_id=2, sku="X-002")


async def test_get_nonexistent_product_raises_not_found(product_service):
    with pytest.raises(ProductNotFoundError):
        await product_service.get_product(9999)


async def test_update_product_category_revalidates_it(product_service):
    product = await _make_product(product_service)

    with pytest.raises(InvalidCategoryError):
        await product_service.update_product(
            product_id=product.id,
            name=None,
            description=None,
            price_cents=None,
            stock=None,
            category_id=2,  # inactive
            status=None,
        )


async def test_update_product_price_and_stock(product_service):
    product = await _make_product(product_service)

    updated = await product_service.update_product(
        product_id=product.id,
        name=None,
        description=None,
        price_cents=2999,
        stock=5,
        category_id=None,
        status=None,
    )

    assert updated.price_cents == 2999
    assert updated.stock == 5


async def test_archive_product_sets_status_archived(product_service):
    product = await _make_product(product_service)

    archived = await product_service.archive_product(product.id)

    assert archived.status == ProductStatus.ARCHIVED


async def test_get_draft_product_404s_for_non_admin(product_service):
    product = await _make_product(
        product_service, sku="DRAFT-001", status=ProductStatus.DRAFT
    )

    with pytest.raises(ProductNotFoundError):
        await product_service.get_product(product.id)

    with pytest.raises(ProductNotFoundError):
        await product_service.get_product(product.id, is_admin=False)


async def test_get_draft_product_visible_to_admin(product_service):
    product = await _make_product(
        product_service, sku="DRAFT-002", status=ProductStatus.DRAFT
    )

    fetched = await product_service.get_product(product.id, is_admin=True)

    assert fetched.id == product.id


async def test_get_archived_product_404s_for_non_admin(product_service):
    product = await _make_product(product_service, sku="ARCH-001")
    await product_service.archive_product(product.id)

    with pytest.raises(ProductNotFoundError):
        await product_service.get_product(product.id)


async def test_list_products_excludes_draft_and_archived_for_non_admin(product_service):
    active = await _make_product(product_service, sku="ACTIVE-001")
    await _make_product(product_service, sku="DRAFT-003", status=ProductStatus.DRAFT)
    archived = await _make_product(product_service, sku="ARCH-002")
    await product_service.archive_product(archived.id)

    results, total = await product_service.list_products(offset=0, limit=20)

    assert total == 1
    assert [p.id for p in results] == [active.id]


async def test_list_products_includes_draft_and_archived_for_admin(product_service):
    active = await _make_product(product_service, sku="ACTIVE-002")
    draft = await _make_product(
        product_service, sku="DRAFT-004", status=ProductStatus.DRAFT
    )
    archived = await _make_product(product_service, sku="ARCH-003")
    await product_service.archive_product(archived.id)

    results, total = await product_service.list_products(
        offset=0, limit=20, is_admin=True
    )

    assert total == 3
    assert {p.id for p in results} == {active.id, draft.id, archived.id}
