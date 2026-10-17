import logging

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    InvalidStateError,
    NotFoundError,
    UnauthorizedError,
    UpstreamServiceError,
)
from app.core.logging_config import mark_logged
from app.core.request_context import REQUEST_ID_HEADER, get_request_id, restore_context
from app.modules.orders.exceptions.order_exceptions import CheckoutNotConfiguredError

logger = logging.getLogger(__name__)


def _error(
    request: Request,
    status_code: int,
    detail: str | list,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """
    Every error body carries the request id. The header is set here too because
    the catch-all 500 handler runs outside RequestContextMiddleware, which is
    what normally adds it.
    """
    request_id = get_request_id(request)
    response = JSONResponse(
        status_code=status_code,
        content={"detail": detail, "request_id": request_id},
        headers=headers,
    )
    if request_id:
        response.headers[REQUEST_ID_HEADER] = request_id
    return response


def register_exception_handlers(app: FastAPI) -> None:

    @app.exception_handler(NotFoundError)
    async def handle_not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        return _error(request, status.HTTP_404_NOT_FOUND, exc.message)

    @app.exception_handler(ConflictError)
    async def handle_conflict(request: Request, exc: ConflictError) -> JSONResponse:
        return _error(request, status.HTTP_409_CONFLICT, exc.message)

    @app.exception_handler(ForbiddenError)
    async def handle_forbidden(request: Request, exc: ForbiddenError) -> JSONResponse:
        return _error(request, status.HTTP_403_FORBIDDEN, exc.message)

    @app.exception_handler(UnauthorizedError)
    async def handle_unauthorized(request: Request, exc: UnauthorizedError) -> JSONResponse:
        return _error(request, status.HTTP_401_UNAUTHORIZED, exc.message, headers={"WWW-Authenticate": "Bearer"})

    @app.exception_handler(BadRequestError)
    async def handle_bad_request(request: Request, exc: BadRequestError) -> JSONResponse:
        return _error(request, status.HTTP_400_BAD_REQUEST, exc.message)

    @app.exception_handler(UpstreamServiceError)
    async def handle_upstream_service_error(
        request: Request, exc: UpstreamServiceError
    ) -> JSONResponse:
        logger.warning("Upstream service error on %s %s: %s", request.method, request.url.path, exc)
        return _error(request, status.HTTP_502_BAD_GATEWAY, exc.message)

    @app.exception_handler(InvalidStateError)
    async def handle_invalid_state(request: Request, exc: InvalidStateError) -> JSONResponse:
        return _error(request, status.HTTP_422_UNPROCESSABLE_ENTITY, exc.message)

    @app.exception_handler(CheckoutNotConfiguredError)
    async def handle_checkout_not_configured(
        request: Request, exc: CheckoutNotConfiguredError
    ) -> JSONResponse:
        # A deployment/config problem, not a client error: log loudly.
        logger.error("Checkout refused: %s", exc.message)
        return _error(request, status.HTTP_503_SERVICE_UNAVAILABLE, exc.message)

    @app.exception_handler(IntegrityError)
    async def handle_integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:

        logger.warning("IntegrityError caught by global handler: %s", exc)
        return _error(
            request, status.HTTP_409_CONFLICT, "This request conflicts with existing data"
        )

    @app.exception_handler(PoolTimeoutError)
    async def handle_pool_exhausted(request: Request, exc: PoolTimeoutError) -> JSONResponse:
        # SQLAlchemy raises this when no database connection became free within
        # db_pool_timeout seconds ("QueuePool limit ... reached"). That is
        # overload, not a bug: tell the client to retry instead of reporting a
        # 500, and log it loudly so it shows up in monitoring.
        logger.error(
            "Database connection pool exhausted on %s %s: %s",
            request.method,
            request.url.path,
            exc,
        )
        return _error(
            request,
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The service is busy. Please retry in a moment.",
            headers={"Retry-After": "1"},
        )

    # FastAPI's own 404 / 405 / 422 bodies keep their shape ({"detail": ...});
    # they just gain "request_id" like every other error.
    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> Response:
        headers = dict(exc.headers) if exc.headers else None
        if exc.status_code in (204, 304) or exc.status_code < 200:
            return Response(status_code=exc.status_code, headers=headers)
        return _error(request, exc.status_code, exc.detail, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error(
            request, status.HTTP_422_UNPROCESSABLE_ENTITY, jsonable_encoder(exc.errors())
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        # Runs outside RequestContextMiddleware (Starlette's ServerErrorMiddleware
        # is the outermost layer), so put the request's log context back first.
        with restore_context(request):
            logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        mark_logged(exc)  # so uvicorn's duplicate, id-less copy of it is dropped
        return _error(
            request, status.HTTP_500_INTERNAL_SERVER_ERROR, "An unexpected error occurred"
        )
