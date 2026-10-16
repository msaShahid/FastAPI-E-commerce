from app.core.exceptions import ConflictError, NotFoundError


class AddressNotFoundError(NotFoundError):
    def __init__(self, address_id: int) -> None:
        self.address_id = address_id
        super().__init__(f"Address with id {address_id} not found")


class AddressLimitReachedError(ConflictError):
    def __init__(self, limit: int) -> None:
        super().__init__(
            f"You can save at most {limit} addresses. Delete one before adding another."
        )
