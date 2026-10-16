from fastapi import APIRouter, Depends, File, Response, UploadFile, status

from app.core.cache import CATEGORIES, cache_headers, get_cache
from app.core.config import get_settings
from app.modules.audit.dependencies.audit_deps import AuditServiceDep
from app.modules.auth.dependencies.auth import AdminUser
from app.modules.categories.dependencies.category_deps import CategoryServiceDep
from app.modules.categories.models.category import Category
from app.modules.categories.schemas.category import (
    CategoryCreate,
    CategoryRead,
    CategoryUpdate,
)
from app.modules.categories.services.category_service import CategoryService
from app.shared.pagination.schemas import PageParams, PaginatedResponse

category_router = APIRouter(prefix="/categories", tags=["categories"])


def _to_category_read(category: Category, service: CategoryService) -> CategoryRead:

    base = CategoryRead.model_validate(category)
    image_url = (
        service.storage.get_url(path=category.image_path)
        if category.image_path
        else None
    )
    return base.model_copy(update={"image_url": image_url})


@category_router.post(
    "", response_model=CategoryRead, status_code=status.HTTP_201_CREATED
)
async def create_category(
    payload: CategoryCreate,
    service: CategoryServiceDep,
    admin: AdminUser,
    audit_service: AuditServiceDep,
) -> CategoryRead:
    category = await service.create_category(
        name=payload.name, description=payload.description
    )

    await audit_service.log(
        actor_user_id=admin.id,
        action="category.created",
        target_type="category",
        target_id=category.id,
        details={"name": category.name},
    )

    return _to_category_read(category, service)


@category_router.get("", response_model=PaginatedResponse[CategoryRead])
async def list_categories(
    service: CategoryServiceDep, params: PageParams = Depends()
) -> Response:
    async def load() -> str:
        categories, total = await service.list_categories(
            offset=params.offset, limit=params.page_size
        )
        return PaginatedResponse(
            items=[_to_category_read(c, service) for c in categories],
            total=total,
            page=params.page,
            page_size=params.page_size,
        ).model_dump_json()

    # The list is identical for every caller, so it is cached per page.
    payload, cache_status = await get_cache().get_or_set(
        CATEGORIES,
        {"page": params.page, "page_size": params.page_size},
        get_settings().cache_ttl_categories_seconds,
        load,
    )
    return Response(
        content=payload,
        media_type="application/json",
        headers=cache_headers(cache_status),
    )


@category_router.get("/{category_id}", response_model=CategoryRead)
async def get_category(category_id: int, service: CategoryServiceDep) -> CategoryRead:
    category = await service.get_category(category_id)
    return _to_category_read(category, service)


@category_router.patch("/{category_id}", response_model=CategoryRead)
async def update_category(
    category_id: int,
    payload: CategoryUpdate,
    service: CategoryServiceDep,
    admin: AdminUser,
    audit_service: AuditServiceDep,
) -> CategoryRead:
    category = await service.update_category(
        category_id=category_id,
        name=payload.name,
        description=payload.description,
        is_active=payload.is_active,
    )

    await audit_service.log(
        actor_user_id=admin.id,
        action="category.updated",
        target_type="category",
        target_id=category.id,
        details=payload.model_dump(exclude_unset=True, exclude_none=True),
    )

    return _to_category_read(category, service)


@category_router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_category(
    category_id: int,
    service: CategoryServiceDep,
    admin: AdminUser,
    audit_service: AuditServiceDep,
) -> None:
    await service.deactivate_category(category_id)

    await audit_service.log(
        actor_user_id=admin.id,
        action="category.deactivated",
        target_type="category",
        target_id=category_id,
    )


@category_router.post("/{category_id}/image", response_model=CategoryRead)
async def upload_category_image(
    category_id: int,
    service: CategoryServiceDep,
    _admin: AdminUser,
    image: UploadFile = File(...),
) -> CategoryRead:
    category = await service.upload_category_image(category_id=category_id, image=image)
    return _to_category_read(category, service)


@category_router.delete("/{category_id}/image", response_model=CategoryRead)
async def delete_category_image(
    category_id: int, service: CategoryServiceDep, _admin: AdminUser
) -> CategoryRead:
    category = await service.delete_category_image(category_id=category_id)
    return _to_category_read(category, service)
