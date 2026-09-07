import pytest

from app.modules.categories.exceptions.category_exceptions import (
    CategoryNameAlreadyExistsError,
    CategoryNotFoundError,
)
from app.modules.categories.services.category_service import CategoryService


@pytest.fixture
def category_service(
    fake_category_repository,
    fake_storage_service,
) -> CategoryService:
    return CategoryService(
        fake_category_repository,
        fake_storage_service,
    )


async def test_create_category_generates_slug(category_service):
    category = await category_service.create_category(
        name="Wireless Headphones",
        description=None,
    )

    assert category.slug == "wireless-headphones"


async def test_create_category_rejects_duplicate_name(category_service):
    await category_service.create_category(
        name="Electronics",
        description=None,
    )

    with pytest.raises(CategoryNameAlreadyExistsError):
        await category_service.create_category(
            name="Electronics",
            description=None,
        )


async def test_slug_collision_gets_suffixed(category_service):
    first = await category_service.create_category(
        name="Sale!!!",
        description=None,
    )

    second = await category_service.create_category(
        name="Sale???",
        description=None,
    )

    assert first.slug == "sale"
    assert second.slug == "sale-2"
    assert first.slug != second.slug


async def test_get_nonexistent_category_raises_not_found(category_service):
    with pytest.raises(CategoryNotFoundError):
        await category_service.get_category(9999)


async def test_update_category_name_regenerates_slug(category_service):
    category = await category_service.create_category(
        name="Old Name",
        description=None,
    )

    assert category.slug == "old-name"

    updated = await category_service.update_category(
        category_id=category.id,
        name="New Name",
        description=None,
        is_active=None,
    )

    assert updated.name == "New Name"
    assert updated.slug == "new-name"


async def test_update_category_to_existing_name_rejected(category_service):
    await category_service.create_category(
        name="Books",
        description=None,
    )

    other = await category_service.create_category(
        name="Movies",
        description=None,
    )

    with pytest.raises(CategoryNameAlreadyExistsError):
        await category_service.update_category(
            category_id=other.id,
            name="Books",
            description=None,
            is_active=None,
        )


async def test_deactivate_category_sets_is_active_false(category_service):
    category = await category_service.create_category(
        name="Toys",
        description=None,
    )

    result = await category_service.deactivate_category(category.id)

    assert result.is_active is False
