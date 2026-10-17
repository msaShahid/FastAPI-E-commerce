from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
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

    # Connection pool. These limits apply PER API PROCESS: with N worker
    # processes (or replicas) the database can see up to
    #     N * (db_pool_size + db_max_overflow)
    # connections, and that total must stay below Postgres's max_connections.
    # See docs/connection-pool.md for how to size them.
    db_pool_size: int = Field(default=5, ge=1)          # connections kept open
    db_max_overflow: int = Field(default=10, ge=0)      # extra, closed again when idle
    db_pool_timeout: int = Field(default=10, ge=1)      # seconds to wait for a free connection
    db_pool_recycle: int = Field(default=1800, ge=-1)   # replace connections older than this (s); -1 = never
    db_pool_pre_ping: bool = True                       # test a connection before using it

    # Set to true ONLY if DATABASE_URL goes through a transaction-mode
    # connection pooler (PgBouncer, including a provider's "-pooler" endpoint)
    # that is not configured for prepared statements and you see errors like
    # 'prepared statement "__asyncpg_stmt_..." does not exist'.
    db_disable_prepared_statements: bool = False

    # --- JWT / Auth --- python -c "import secrets; print(secrets.token_hex(32))"

    jwt_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    # --- CORS ---
    # The API is called with credentials (cookies -- see the guest cart
    # token, and the Authorization header) from CORSMiddleware in
    # main.py, which is configured with allow_credentials=True. Per the
    # CORS spec, a browser refuses to expose a credentialed response to
    # the page's JS at all when the server's allowed origin is the
    # wildcard "*" -- so pairing "*" with allow_credentials=True doesn't
    # relax anything, it just silently breaks every cross-origin request
    # that depends on cookies or auth headers. The validator below turns
    # that footgun into a startup error instead of a confusing runtime
    # failure discovered by a frontend developer.
    #
    # Must be set as a JSON array in the environment, e.g.:
    #   CORS_ORIGINS=["https://shop.example.com","https://admin.example.com"]
    # (a plain comma-separated string is NOT accepted here -- pydantic-settings
    # parses a list-typed field from an env var as JSON, so anything else
    # raises a validation error at startup rather than a silently-wrong config.)
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

    # --- GST master data (see app/modules/tax) ---
    # Default: a tax-master record must be approved by a DIFFERENT administrator
    # than its creator. A business with a single administrator can set this to true:
    # self-approval then requires a written note and is flagged in the audit log.
    # It gives no independent control; the real safeguard is the GST professional's
    # written approval, recorded in source_reference.
    tax_master_allow_self_approval: bool = False

    # --- Dev seeding (see scripts/seed.py) ---
    seed_admin_email: str = "admin@example.com"
    seed_admin_password: str = "AdminPassword123!"
    seed_user_email: str = "user@example.com"
    seed_user_password: str = "UserPassword123!"

     # --- Payments (Stripe) ---
    stripe_secret_key: str
    stripe_webhook_secret: str

    # --- Orders ---
    # Checkout decrements stock immediately (see OrderService.checkout),
    # so a PENDING order that never gets paid holds that stock forever
    # unless something releases it. A periodic job calls
    # OrderService.cancel_stale_pending_orders() to cancel and restock
    # any order that has been PENDING longer than this.
    pending_order_timeout_minutes: int = 30

    # --- Shipping & tax (flat-rate; no carrier/tax-service integration) ---
    # Deliberately UNSET by default. Checkout refuses to run (HTTP 503) until
    # flat_shipping_cents and tax_rate_percent are both set explicitly, so a
    # taxable amount can never silently default to zero or to a placeholder
    # rate. Setting a value to 0 is allowed but must be a conscious decision.
    # These are interim flat settings; destination-based tax arrives with the
    # GST milestone. Do not set a rate that has not been approved.
    flat_shipping_cents: int | None = Field(default=None, ge=0)
    free_shipping_threshold_cents: int | None = Field(default=None, ge=0)  # None/0 = no free shipping
    # What the free-shipping threshold is compared with. Required whenever a
    # threshold is set, so the owner states the rule instead of inheriting one.
    # "subtotal" = merchandise subtotal as priced at checkout (before shipping/tax).
    free_shipping_threshold_basis: Literal["subtotal"] | None = None
    tax_rate_percent: Decimal | None = Field(default=None, ge=0, le=100)

    # --- Guest cart ---
    # How long an anonymous shopper's cart cookie lives before it expires.
    # On login/register, a guest cart is merged into the user's cart and
    # the cookie is cleared, so this mostly bounds an abandoned guest cart.
    guest_cart_cookie_max_age_days: int = 30

    # --- Rate limiting ---
    # Backing store for slowapi's rate-limit counters. In-memory storage
    # (slowapi's default when this is unset) keeps a separate counter per
    # process, so with more than one API worker/replica each one enforces
    # the limit independently and the *effective* limit is multiplied by
    # the worker count. Redis gives every worker/replica one shared
    # counter, so "5/minute" actually means 5/minute across the whole
    # deployment.
    redis_url: str = "redis://redis:6379/0"

    # --- Logging ---
    # json: one JSON object per line (use this wherever logs are collected).
    # text: readable lines for local development.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "text"] = "json"

    # --- Response cache (Redis) ---
    # Caches the public category and product LISTS (see app/core/cache.py and
    # docs/caching.md). Redis being slow or down never breaks a request: the
    # cache is skipped and the database answers, as if caching were off.
    cache_enabled: bool = True
    cache_key_prefix: str = "shop"  # lets several environments share one Redis
    cache_ttl_categories_seconds: int = Field(default=300, ge=1)
    cache_ttl_products_seconds: int = Field(default=60, ge=1)
    # Per-command Redis timeout. Kept short on purpose: a cache that is slower
    # than the query it replaces is worse than no cache.
    cache_redis_timeout_seconds: float = Field(default=0.25, gt=0)
    # After a Redis failure, skip Redis entirely for this long.
    cache_failure_cooldown_seconds: float = Field(default=5.0, ge=0)

    # --- Background jobs (see app/core/jobs and docs/background-jobs.md) ---
    # Run the worker inside the API process instead of as its own process.
    # Convenient for development and small deployments; for production prefer
    # `python -m scripts.run_worker` so a slow job can never compete with request
    # handling for CPU and so the worker can be scaled on its own.
    jobs_embedded_worker: bool = False
    jobs_concurrency: int = Field(default=2, ge=1)  # jobs a worker runs at once
    jobs_poll_interval_seconds: float = Field(default=1.0, gt=0)  # sleep when idle
    jobs_default_max_attempts: int = Field(default=5, ge=1)
    jobs_job_timeout_seconds: float = Field(default=60.0, gt=0)  # one attempt's time limit
    # How long a claimed job is invisible to other workers. If its worker dies,
    # the job is retried after this long. Must be longer than the job timeout.
    jobs_lock_seconds: float = Field(default=300.0, gt=0)
    # Retry delay after attempt n: base * 2**(n-1), capped.
    jobs_retry_base_seconds: float = Field(default=30.0, ge=0)
    jobs_retry_max_seconds: float = Field(default=3600.0, ge=0)

    @model_validator(mode="after")
    def _threshold_needs_a_basis(self) -> "Settings":
        if self.free_shipping_threshold_cents and self.free_shipping_threshold_basis is None:
            raise ValueError(
                "free_shipping_threshold_basis must be set (currently: 'subtotal') "
                "when free_shipping_threshold_cents is set"
            )
        return self

    @model_validator(mode="after")
    def _lock_outlasts_job_timeout(self) -> "Settings":
        if self.jobs_lock_seconds <= self.jobs_job_timeout_seconds:
            raise ValueError(
                "jobs_lock_seconds must be longer than jobs_job_timeout_seconds, otherwise "
                "another worker could pick up a job that is still running."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """
    Returns a cached Settings instance.

    lru_cache ensures the .env file / environment is only parsed once,
    and every part of the app that calls get_settings() shares the
    same object instead of re-reading and re-validating config repeatedly.
    """
    return Settings()
