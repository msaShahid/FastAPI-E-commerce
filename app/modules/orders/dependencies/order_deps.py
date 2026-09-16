from typing import Annotated

from fastapi import Depends

from app.core.database import DbSession
from app.modules.cart.dependencies.cart_deps import get_cart_repository
from app.modules.cart.repositories.cart_repository import CartRepository
from app.modules.orders.repositories.order_repository import OrderRepository
from app.modules.orders.services.order_service import OrderService
from app.modules.products.dependencies.product_deps import get_product_repository
from app.modules.products.repositories.product_repository import ProductRepository


def get_order_repository(db: DbSession) -> OrderRepository:
    return OrderRepository(db)


def get_order_service(
    repository: Annotated[OrderRepository, Depends(get_order_repository)],
    cart_repository: Annotated[CartRepository, Depends(get_cart_repository)],
    product_repository: Annotated[ProductRepository, Depends(get_product_repository)],
) -> OrderService:

    return OrderService(repository, cart_repository, product_repository)


OrderServiceDep = Annotated[OrderService, Depends(get_order_service)]
