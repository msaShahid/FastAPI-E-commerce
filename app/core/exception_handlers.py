import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import (
    BadRequestError,
    ConflictError,
    ErrorResponse,
    ForbiddenError,
    InvalidStateError,
    NotFoundError,
    UnauthorizedError,
    UpstreamServiceError,
)

logger = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:

    @app.exception_handler(NotFoundError)
    async def handle_not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content=ErrorResponse(detail=exc.message).model_dump(),
        )

    @app.exception_handler(ConflictError)
    async def handle_conflict(request: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=ErrorResponse(detail=exc.message).model_dump(),
        )

    @app.exception_handler(ForbiddenError)
    async def handle_forbidden(request: Request, exc: ForbiddenError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content=ErrorResponse(detail=exc.message).model_dump(),
        )

    @app.exception_handler(UnauthorizedError)
    async def handle_unauthorized(request: Request, exc: UnauthorizedError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content=ErrorResponse(detail=exc.message).model_dump(),
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(BadRequestError)
    async def handle_bad_request(request: Request, exc: BadRequestError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=ErrorResponse(detail=exc.message).model_dump(),
        )

    @app.exception_handler(UpstreamServiceError)
    async def handle_upstream_service_error(
        request: Request, exc: UpstreamServiceError
    ) -> JSONResponse:
        logger.warning("Upstream service error on %s %s: %s", request.method, request.url.path, exc)
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content=ErrorResponse(detail=exc.message).model_dump(),
        )

    @app.exception_handler(InvalidStateError)
    async def handle_invalid_state(request: Request, exc: InvalidStateError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=ErrorResponse(detail=exc.message).model_dump(),
        )

    @app.exception_handler(IntegrityError)
    async def handle_integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:

        logger.warning("IntegrityError caught by global handler: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=ErrorResponse(
                detail="This request conflicts with existing data"
            ).model_dump(),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:

        logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=ErrorResponse(detail="An unexpected error occurred").model_dump(),
        )