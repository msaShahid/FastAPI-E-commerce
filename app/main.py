import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import close_cache
from app.core.config import get_settings
from app.core.database import async_session_factory, get_db
from app.core.exception_handlers import register_exception_handlers
from app.core.jobs.worker import run_worker
from app.core.logging_config import configure_logging
from app.core.rate_limit import limiter, warn_if_forwarded_headers_untrusted
from app.core.request_context import REQUEST_ID_HEADER, RequestContextMiddleware
from app.core.security_headers import SecurityHeadersMiddleware
from app.graphql.schema import graphql_router
from app.modules.addresses.api.router import address_router
from app.modules.audit.api.router import audit_router
from app.modules.auth.api.router import auth_router
from app.modules.cart.api.router import cart_router
from app.modules.categories.api.router import category_router
from app.modules.orders.api.router import order_router
from app.modules.payments.api.lifecycle_router import lifecycle_router
from app.modules.payments.api.provider_router import provider_router
from app.modules.payments.api.router import payment_router
from app.modules.playground.api.router import playground_router
from app.modules.products.api.router import product_router
from app.modules.store.api.router import store_router
from app.modules.tax.api.router import tax_router
from app.modules.users.api.router import users_router

settings = get_settings()
configure_logging(settings.log_level, settings.log_format)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    warn_if_forwarded_headers_untrusted()
    stop_worker = asyncio.Event()
    worker_task: asyncio.Task | None = None
    if settings.jobs_embedded_worker:
        from app import jobs as _jobs  # noqa: F401  (registers the job handlers)

        logger.info("Running the background job worker inside the API process")
        worker_task = asyncio.create_task(
            run_worker(stop_worker, session_factory=async_session_factory)
        )
    yield
    if worker_task is not None:
        # Let jobs that are mid-run finish, but do not hang shutdown on them.
        stop_worker.set()
        try:
            await asyncio.wait_for(worker_task, timeout=settings.jobs_job_timeout_seconds)
        except TimeoutError:
            logger.warning("Job worker did not stop in time; cancelling it")
            worker_task.cancel()
    # Close the cache's Redis connection pool on shutdown.
    await close_cache()


app = FastAPI(
    lifespan=lifespan,
    title="FastAPI E-Commerce",
    version="0.1.0",
    description="Production ready e-commerce backend.",
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # Lets browser JavaScript read the id to show in an error message.
    expose_headers=[REQUEST_ID_HEADER],
)
# Added last = outermost: every response, including CORS preflights, gets an id.
app.add_middleware(RequestContextMiddleware)


app.mount("/media", StaticFiles(directory="media"), name="media")

register_exception_handlers(app)

app.include_router(playground_router, prefix=settings.api_v1_prefix)
app.include_router(auth_router, prefix=settings.api_v1_prefix)
app.include_router(users_router, prefix=settings.api_v1_prefix)
app.include_router(address_router, prefix=settings.api_v1_prefix)
app.include_router(category_router, prefix=settings.api_v1_prefix)
app.include_router(product_router, prefix=settings.api_v1_prefix)
app.include_router(cart_router, prefix=settings.api_v1_prefix)
app.include_router(order_router, prefix=settings.api_v1_prefix)
app.include_router(payment_router, prefix=settings.api_v1_prefix)
app.include_router(audit_router, prefix=settings.api_v1_prefix)
app.include_router(tax_router, prefix=settings.api_v1_prefix)
app.include_router(store_router, prefix=settings.api_v1_prefix)
app.include_router(lifecycle_router, prefix=settings.api_v1_prefix)
app.include_router(provider_router, prefix=settings.api_v1_prefix)


app.include_router(graphql_router, prefix="/graphql")

@app.get("/health", tags=["health"])
def health_check() -> dict:
    return {
        "status": "ok",
        "environment": settings.environment,
    }


@app.get("/health/db", tags=["health"])
async def health_check_db(db: Annotated[AsyncSession, Depends(get_db)]) -> dict:

    result = await db.execute(text("SELECT 1"))

    return {
        "status": "ok",
        "database": "reachable",
        "result": result.scalar(),
    }


@app.get("/", tags=["Home"])
def home() -> dict:
    return {
        "message": "Welcome to FastAPI Project",
        "app_name": app.title,
        "version": app.version,
    }
