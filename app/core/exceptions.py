from pydantic import BaseModel


class ErrorResponse(BaseModel):

    detail: str


class AppError(Exception):

    def __init__(self, message: str = "An error occurred") -> None:
        self.message = message
        super().__init__(message)


class NotFoundError(AppError):

    def __init__(self, message: str = "Resource not found") -> None:
        super().__init__(message)


class ConflictError(AppError):

    def __init__(self, message: str = "Resource already exists") -> None:
        super().__init__(message)


class ForbiddenError(AppError):

    def __init__(self, message: str = "You don't have permission to do this") -> None:
        super().__init__(message)


class UnauthorizedError(AppError):

    def __init__(self, message: str = "Authentication required") -> None:
        super().__init__(message)


class InvalidStateError(AppError):

    def __init__(self, message: str = "Invalid request") -> None:
        super().__init__(message)

class BadRequestError(AppError):

    def __init__(self, message: str = "Bad request") -> None:
        super().__init__(message)

class UpstreamServiceError(AppError):

    def __init__(self, message: str = "An upstream service failed") -> None:
        super().__init__(message)

class InvalidImageError(InvalidStateError):

    def __init__(self, message: str) -> None:
        super().__init__(message)