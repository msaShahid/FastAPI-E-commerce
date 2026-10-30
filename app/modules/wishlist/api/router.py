"""The signed-in customer's wishlist (registered customers only)."""

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.core.database import DbSession
from app.modules.auth.dependencies.auth import CurrentUser
from app.modules.products.api.router import _to_product_read
from app.modules.products.dependencies.product_deps import ProductServiceDep
from app.modules.products.schemas.product import ProductRead
from app.modules.wishlist.schemas.wishlist import WishlistState
from app.modules.wishlist.services.wishlist_service import WishlistService
from app.shared.pagination.schemas import PageParams, PaginatedResponse

wishlist_router = APIRouter(prefix="/wishlist", tags=["wishlist"])


def get_wishlist_service(db: DbSession) -> WishlistService:
    return WishlistService(db)


ServiceDep = Annotated[WishlistService, Depends(get_wishlist_service)]


@wishlist_router.get("", response_model=PaginatedResponse[ProductRead])
async def my_wishlist(user: CurrentUser, service: ServiceDep, products: ProductServiceDep, params: PageParams = Depends()):
    """Saved products that are on sale, most recently saved first, shown like the product list (prices, availability, variants' range)."""
    items, total = await service.list_products(user, params.offset, params.page_size)
    await products.attach_variant_views(items)
    return PaginatedResponse[ProductRead](
        items=[_to_product_read(p, products) for p in items], total=total, page=params.page, page_size=params.page_size
    )


@wishlist_router.get("/ids", response_model=list[int])
async def my_wishlist_ids(user: CurrentUser, service: ServiceDep):
    """Just the product ids, to mark which products on a page are saved."""
    return await service.product_ids(user)


@wishlist_router.put("/{product_id}", response_model=WishlistState)
async def add_to_wishlist(product_id: int, user: CurrentUser, service: ServiceDep):
    return await service.add(user, product_id)


@wishlist_router.delete("/{product_id}", response_model=WishlistState, status_code=status.HTTP_200_OK)
async def remove_from_wishlist(product_id: int, user: CurrentUser, service: ServiceDep):
    return await service.remove(user, product_id)
