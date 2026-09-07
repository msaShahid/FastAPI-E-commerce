from app.core.exceptions import ConflictError, NotFoundError


class CategoryNotFoundError(NotFoundError):
    def __init__(self, category_id: int) -> None:
        self.category_id = category_id
        super().__init__(
            f"Category with id {category_id} not found"
        )


class CategoryNameAlreadyExistsError(ConflictError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"Category with name '{name}' already exists"
        )