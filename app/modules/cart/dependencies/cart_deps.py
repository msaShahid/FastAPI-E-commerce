from typing import Annotated

from fastapi import Depends

from app.core.database import DbSession
from app.modules.cart.repositories.cart_repository import CartRepository
from app.modules.cart.services.cart_service import CartService
from app.modules.products.dependencies.product_deps import get_product_repository
from app.modules.products.repositories.product_repository import ProductRepository


def get_cart_repository(db: DbSession) -> CartRepository:
    return CartRepository(db)


def get_cart_service(
    repository: Annotated[CartRepository, Depends(get_cart_repository)],
    product_repository: Annotated[ProductRepository, Depends(get_product_repository)],
) -> CartService:
    return CartService(repository, product_repository)


CartServiceDep = Annotated[CartService, Depends(get_cart_service)]
