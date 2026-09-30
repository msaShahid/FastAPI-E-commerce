from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Centralized, typed application configuration.

    Values are loaded from environment variables (and, in development,
    from a `.env` file). Pydantic validates types and raises a clear
    error at startup if something required is missing or malformed,
    instead of failing later with a confusing runtime error.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- App ---
    environment: str = "development"  # development | testing | staging | production
    api_v1_prefix: str = "/api/v1"

    # --- Database ---
    database_url: str

    # --- JWT / Auth --- python -c "import secrets; print(secrets.token_hex(32))"

    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    # --- CORS (kept minimal for now, revisited later) ---
    cors_origins: list[str] = ["http://localhost:3000"]

    @field_validator("cors_origins")
    @classmethod
    def _no_wildcard_origin(cls, value: list[str]) -> list[str]:
        if "*" in value:
            raise ValueError(
                "cors_origins must not contain '*' -- CORSMiddleware is configured "
                "with allow_credentials=True, and browsers refuse to honor a "
                "wildcard origin on a credentialed request anyway. List the exact "
                "origin(s) that should be allowed instead, e.g. "
                '["https://shop.example.com"].'
            )
        return value

    # --- Dev seeding (see scripts/seed.py) ---
    seed_admin_email: str = "admin@example.com"
    seed_admin_password: str = "AdminPassword123!"
    seed_user_email: str = "user@example.com"
    seed_user_password: str = "UserPassword123!"

    # --- Payments (Stripe) ---
    stripe_secret_key: str
    stripe_webhook_secret: str

    # --- Orders ---
    pending_order_timeout_minutes: int = 30

    # --- Shipping & tax (flat-rate; no carrier/tax-service integration) ---
    flat_shipping_cents: int = 500
    free_shipping_threshold_cents: int = 5000
    tax_rate_percent: float = 8.0

    # --- Guest cart ---
    # How long an anonymous shopper's cart cookie lives before it expires.
    guest_cart_cookie_max_age_days: int = 30

    # --- Rate limiting ---
    redis_url: str = "redis://redis:6379/0"


@lru_cache
def get_settings() -> Settings:
    """
    Returns a cached Settings instance.

    lru_cache ensures the .env file / environment is only parsed once,
    and every part of the app that calls get_settings() shares the
    same object instead of re-reading and re-validating config repeatedly.
    """
    return Settings()
