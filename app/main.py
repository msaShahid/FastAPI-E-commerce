from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.exception_handlers import register_exception_handlers
from app.core.security_headers import SecurityHeadersMiddleware
from app.modules.auth.api.router import auth_router
from app.modules.categories.api.router import category_router
from app.modules.playground.api.router import playground_router
from app.modules.products.api.router import product_router
from app.modules.users.api.router import users_router

settings = get_settings()

app = FastAPI(
    title="FastAPI Shop",
    version="0.1.0",
    description="A learning project: production-style e-commerce backend.",
)


app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.mount("/media", StaticFiles(directory="media"), name="media")

register_exception_handlers(app)

app.include_router(playground_router, prefix=settings.api_v1_prefix)
app.include_router(auth_router, prefix=settings.api_v1_prefix)
app.include_router(users_router, prefix=settings.api_v1_prefix)
app.include_router(category_router, prefix=settings.api_v1_prefix)
app.include_router(product_router, prefix=settings.api_v1_prefix)


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
