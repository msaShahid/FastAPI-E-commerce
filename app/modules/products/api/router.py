from fastapi import APIRouter, Depends, File, UploadFile, status

from app.modules.auth.dependencies.auth import AdminUser, OptionalUser
from app.modules.products.dependencies.product_deps import ProductServiceDep
from app.modules.products.models.product import Product
from app.modules.products.schemas.product import (
    ProductCreate,
    ProductQueryParams,
    ProductRead,
    ProductUpdate,
)
from app.modules.products.services.product_service import ProductService
from app.shared.enums.roles import UserRole
from app.shared.pagination.schemas import PaginatedResponse


def _is_admin(current_user) -> bool:
    return current_user is not None and current_user.role == UserRole.ADMIN

product_router = APIRouter(prefix="/products", tags=["products"])


def _to_product_read(product: Product, service: ProductService) -> ProductRead:
    base = ProductRead.model_validate(product)
    image_url = (
        service.storage.get_url(path=product.image_path) if product.image_path else None
    )
    return base.model_copy(update={"image_url": image_url})


@product_router.post("", response_model=ProductRead, status_code=status.HTTP_201_CREATED)
async def create_product(
    payload: ProductCreate, service: ProductServiceDep, _admin: AdminUser
) -> ProductRead:
    product = await service.create_product(
        name=payload.name,
        description=payload.description,
        price_cents=payload.price_cents,
        sku=payload.sku,
        stock=payload.stock,
        category_id=payload.category_id,
        status=payload.status,
    )
    return _to_product_read(product, service)


@product_router.get("", response_model=PaginatedResponse[ProductRead])
async def list_products(
    service: ProductServiceDep,
    current_user: OptionalUser,
    params: ProductQueryParams = Depends(),
) -> PaginatedResponse[ProductRead]:
    products, total = await service.list_products(
        offset=params.offset,
        limit=params.page_size,
        category_id=params.category_id,
        search=params.search,
        min_price=params.min_price,
        max_price=params.max_price,
        sort=params.sort,
        is_admin=_is_admin(current_user),
    )
    return PaginatedResponse(
        items=[_to_product_read(p, service) for p in products],
        total=total,
        page=params.page,
        page_size=params.page_size,
    )


@product_router.get("/{product_id}", response_model=ProductRead)
async def get_product(
    product_id: int, service: ProductServiceDep, current_user: OptionalUser
) -> ProductRead:
    product = await service.get_product(product_id, is_admin=_is_admin(current_user))
    return _to_product_read(product, service)


@product_router.patch("/{product_id}", response_model=ProductRead)
async def update_product(
    product_id: int,
    payload: ProductUpdate,
    service: ProductServiceDep,
    _admin: AdminUser,
) -> ProductRead:
    product = await service.update_product(
        product_id=product_id,
        name=payload.name,
        description=payload.description,
        price_cents=payload.price_cents,
        stock=payload.stock,
        category_id=payload.category_id,
        status=payload.status,
    )
    return _to_product_read(product, service)


@product_router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def archive_product(
    product_id: int, service: ProductServiceDep, _admin: AdminUser
) -> None:
    await service.archive_product(product_id)


@product_router.post("/{product_id}/image", response_model=ProductRead)
async def upload_product_image(
    product_id: int,
    service: ProductServiceDep,
    _admin: AdminUser,
    image: UploadFile = File(...),
) -> ProductRead:
    product = await service.upload_product_image(product_id=product_id, image=image)
    return _to_product_read(product, service)


@product_router.delete("/{product_id}/image", response_model=ProductRead)
async def delete_product_image(
    product_id: int, service: ProductServiceDep, _admin: AdminUser
) -> ProductRead:
    product = await service.delete_product_image(product_id=product_id)
    return _to_product_read(product, service)