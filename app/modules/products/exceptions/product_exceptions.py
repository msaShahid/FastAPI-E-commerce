from app.core.exceptions import ConflictError, InvalidStateError, NotFoundError


class SkuAlreadyExistsError(ConflictError):
    def __init__(self, sku: str) -> None:
        super().__init__(f"A product with SKU '{sku}' already exists")


class ProductNotFoundError(NotFoundError):
    def __init__(self, product_id: int) -> None:
        super().__init__(f"Product with id {product_id} not found")


class InvalidCategoryError(ConflictError):
    def __init__(self, category_id: int) -> None:
        super().__init__(f"Category with id {category_id} does not exist or is inactive")


class InvalidTaxClassificationError(InvalidStateError):
    code = "tax_classification_invalid"

    def __init__(self, classification_id: int) -> None:
        super().__init__(f"Tax classification {classification_id} does not exist")


class ClassificationNotApprovedError(InvalidStateError):
    code = "classification_not_approved"

    def __init__(self, classification_id: int) -> None:
        super().__init__(f"Tax classification {classification_id} is not approved")


class ProductTaxRequiredError(InvalidStateError):
    code = "product_tax_classification_required"

    def __init__(self) -> None:
        super().__init__("An active product needs an approved tax classification")
