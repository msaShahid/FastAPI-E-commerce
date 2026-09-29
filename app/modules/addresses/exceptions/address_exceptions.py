from app.core.exceptions import NotFoundError


class AddressNotFoundError(NotFoundError):
    def __init__(self, address_id: int) -> None:
        self.address_id = address_id
        super().__init__(f"Address with id {address_id} not found")