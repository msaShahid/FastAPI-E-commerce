from app.core.exceptions import ConflictError, InvalidStateError, NotFoundError


class SkuAlreadyExistsError(ConflictError):
    def __init__(self) -> None:
        super().__init__("A product with this SKU already exists")


class ProductNotFoundError(NotFoundError):
    def __init__(self, product_id: int) -> None:
        super().__init__(f"Product with id {product_id} not found")


class InvalidCategoryError(ConflictError):
    def __init__(self, category_id: int) -> None:
        super().__init__(f"Category with id {category_id} does not exist or is inactive")