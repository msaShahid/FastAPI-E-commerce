from fastapi import APIRouter, Depends, File, Response, UploadFile, status

from app.core.cache import PRODUCTS, cache_headers, get_cache
from app.core.config import get_settings
from app.modules.audit.dependencies.audit_deps import AuditServiceDep
from app.modules.auth.dependencies.auth import AdminUser, OptionalUser
from app.modules.products.dependencies.product_deps import ProductServiceDep
from app.modules.products.models.product import Product
from app.modules.products.schemas.product import (
    ProductCreate,
    ProductQueryParams,
    ProductRead,
    ProductUpdate,
)
from app.modules.products.services.product_service import UNSET, ProductService
from app.shared.enums.roles import UserRole
from app.shared.enums.tax import product_tax_state
from app.shared.pagination.schemas import PaginatedResponse


def _is_admin(current_user) -> bool:
    return current_user is not None and current_user.role == UserRole.ADMIN

product_router = APIRouter(prefix="/products", tags=["products"])


def _to_product_read(
    product: Product, service: ProductService, *, include_tax: bool = False
) -> ProductRead:
    """Tax fields are administrator-only: everyone else (and every cached public view) gets null."""
    base = ProductRead.model_validate(product)
    image_url = (
        service.storage.get_url(path=product.image_path) if product.image_path else None
    )
    update: dict = {"image_url": image_url}
    if include_tax:
        classification = product.tax_classification
        update["tax_state"] = product_tax_state(classification.status if classification else None)
    else:
        update.update(unit=None, tax_classification=None, tax_state=None)
    return base.model_copy(update=update)


@product_router.post("", response_model=ProductRead, status_code=status.HTTP_201_CREATED)
async def create_product(
    payload: ProductCreate,
    service: ProductServiceDep,
    admin: AdminUser,
    audit_service: AuditServiceDep,
) -> ProductRead:
    product = await service.create_product(
        name=payload.name,
        description=payload.description,
        price_cents=payload.price_cents,
        sku=payload.sku,
        stock=payload.stock,
        category_id=payload.category_id,
        status=payload.status,
        tax_classification_id=payload.tax_classification_id,
        unit=payload.unit,
    )

    await audit_service.log(
        actor_user_id=admin.id,
        action="product.created",
        target_type="product",
        target_id=product.id,
        details={
            "name": product.name,
            "sku": product.sku,
            "tax_classification_id": product.tax_classification_id,
        },
    )

    return _to_product_read(product, service, include_tax=True)


@product_router.get("", response_model=PaginatedResponse[ProductRead])
async def list_products(
    service: ProductServiceDep,
    current_user: OptionalUser,
    params: ProductQueryParams = Depends(),
) -> Response:
    is_admin = _is_admin(current_user)

    async def load() -> str:
        products, total = await service.list_products(
            offset=params.offset,
            limit=params.page_size,
            category_id=params.category_id,
            search=params.search,
            min_price=params.min_price,
            max_price=params.max_price,
            sort=params.sort,
            is_admin=is_admin,
        )
        return PaginatedResponse(
            items=[_to_product_read(p, service, include_tax=is_admin) for p in products],
            total=total,
            page=params.page,
            page_size=params.page_size,
        ).model_dump_json()

    # Cached only for the public view. Admins also see draft/archived
    # products, so they always query the database. Free-text searches are
    # not cached either: nearly every one is different, so they would fill
    # Redis with entries that are never read again.
    payload, cache_status = await get_cache().get_or_set(
        PRODUCTS,
        {
            "page": params.page,
            "page_size": params.page_size,
            "category_id": params.category_id,
            "min_price": params.min_price,
            "max_price": params.max_price,
            "sort": params.sort,
        },
        get_settings().cache_ttl_products_seconds,
        load,
        cacheable=not is_admin and not params.search,
    )
    return Response(
        content=payload,
        media_type="application/json",
        headers=cache_headers(cache_status),
    )


@product_router.get("/{product_id}", response_model=ProductRead)
async def get_product(
    product_id: int, service: ProductServiceDep, current_user: OptionalUser
) -> ProductRead:
    is_admin = _is_admin(current_user)
    product = await service.get_product(product_id, is_admin=is_admin)
    return _to_product_read(product, service, include_tax=is_admin)


@product_router.patch("/{product_id}", response_model=ProductRead)
async def update_product(
    product_id: int,
    payload: ProductUpdate,
    service: ProductServiceDep,
    admin: AdminUser,
    audit_service: AuditServiceDep,
) -> ProductRead:
    product = await service.update_product(
        product_id=product_id,
        name=payload.name,
        description=payload.description,
        price_cents=payload.price_cents,
        stock=payload.stock,
        category_id=payload.category_id,
        status=payload.status,
        # an explicit null un-assigns; an omitted field leaves the value unchanged
        tax_classification_id=(
            payload.tax_classification_id
            if "tax_classification_id" in payload.model_fields_set
            else UNSET
        ),
        unit=payload.unit if "unit" in payload.model_fields_set else UNSET,
    )

    await audit_service.log(
        actor_user_id=admin.id,
        action="product.updated",
        target_type="product",
        target_id=product.id,
        details=payload.model_dump(exclude_unset=True, exclude_none=True),
    )
    if service.tax_assignment_change is not None:
        old, new = service.tax_assignment_change
        await audit_service.log(
            actor_user_id=admin.id,
            action="product.tax_assignment_changed",
            target_type="product",
            target_id=product.id,
            details={"from": old, "to": new},
        )

    return _to_product_read(product, service, include_tax=True)


@product_router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_product(
    product_id: int,
    service: ProductServiceDep,
    admin: AdminUser,
    audit_service: AuditServiceDep,
) -> None:
    await service.archive_product(product_id)

    await audit_service.log(
        actor_user_id=admin.id,
        action="product.archived",
        target_type="product",
        target_id=product_id,
    )


@product_router.post("/{product_id}/image", response_model=ProductRead)
async def upload_product_image(
    product_id: int,
    service: ProductServiceDep,
    _admin: AdminUser,
    image: UploadFile = File(...),
) -> ProductRead:
    product = await service.upload_product_image(product_id=product_id, image=image)
    return _to_product_read(product, service, include_tax=True)


@product_router.delete("/{product_id}/image", response_model=ProductRead)
async def delete_product_image(
    product_id: int, service: ProductServiceDep, _admin: AdminUser
) -> ProductRead:
    product = await service.delete_product_image(product_id=product_id)
    return _to_product_read(product, service, include_tax=True)
