from pydantic import BaseModel


class WishlistState(BaseModel):
    product_id: int
    in_wishlist: bool
    count: int
