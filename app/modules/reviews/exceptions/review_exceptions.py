from app.core.exceptions import ConflictError, ForbiddenError, InvalidStateError, NotFoundError


class ReviewNotFoundError(NotFoundError):
    code = "review_not_found"

    def __init__(self) -> None:
        super().__init__("Review not found")


class ReviewRequiresPurchaseError(ForbiddenError):
    code = "review_requires_purchase"

    def __init__(self) -> None:
        super().__init__("Only customers who bought this product (a paid or shipped order) can review it")


class AlreadyReviewedError(ConflictError):
    code = "already_reviewed"

    def __init__(self) -> None:
        super().__init__("You have already reviewed this product. Edit your review instead.")


class ReviewHiddenError(ForbiddenError):
    code = "review_hidden"

    def __init__(self) -> None:
        super().__init__("This review was hidden by the store and can no longer be edited")


class InvalidReviewError(InvalidStateError):
    code = "invalid_review"


class WishlistFullError(ConflictError):
    code = "wishlist_full"

    def __init__(self, limit: int) -> None:
        super().__init__(f"A wishlist holds at most {limit} products")
