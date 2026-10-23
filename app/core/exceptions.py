from pydantic import BaseModel


class ErrorResponse(BaseModel):

    detail: str
    # Also sent in the X-Request-ID header. Quote it to support; it finds the logs.
    request_id: str | None = None


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


class ServiceUnavailableError(AppError):
    """The service cannot do this right now because of its own configuration/state (HTTP 503)."""

    code: str | None = None

    def __init__(self, message: str = "Service unavailable") -> None:
        super().__init__(message)


class UpstreamServiceError(AppError):
    """An external service we depend on (a payment provider, etc.) failed
    or is unreachable. Distinct from BadRequestError: the caller did
    nothing wrong here, so this maps to 502, not 400/422."""

    # What a CLIENT is told. Always a fixed sentence: `message` (often the upstream's own text) is for logs and operators only.
    public_message: str = "An upstream service is unavailable. Please try again later."

    def __init__(self, message: str = "An upstream service failed") -> None:
        super().__init__(message)


class InvalidImageError(InvalidStateError):

    def __init__(self, message: str) -> None:
        super().__init__(message)