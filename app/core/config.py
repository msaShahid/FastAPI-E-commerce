import re
from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENVIRONMENTS = frozenset({"development", "testing", "staging", "production"})
# Deployments that real people or real money can reach: secrets there must be genuinely secret.
HARDENED_ENVIRONMENTS = frozenset({"staging", "production"})
JWT_HMAC_ALGORITHMS = frozenset({"HS256", "HS384", "HS512"})
MIN_JWT_SECRET_LENGTH = 32
MIN_WEBHOOK_SECRET_LENGTH = 16
# Fragments that mark a value as a template placeholder rather than a real secret.
_PLACEHOLDER = re.compile(
    r"change[-_]?me|replace[-_]?me|your[-_]|placeholder|example|\*{3,}|<.*>|xxxx|todo|dummy|not[-_]?set",
    re.IGNORECASE,
)


class ConfigurationError(RuntimeError):
    """Unsafe configuration. Deliberately NOT a ValueError: pydantic echoes the offending input (with real secrets) in a
    ValidationError, so these checks raise this instead and their messages name only the setting."""


def looks_like_placeholder(value: str) -> bool:
    """True for template values (CHANGE_ME, ****, <secret>, ...) and for degenerate ones (one repeated character)."""
    return bool(_PLACEHOLDER.search(value)) or len(set(value)) < 6


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
    # Log every SQL statement WITH its bound parameters (customer data, hashes). Off by default; an explicit local-debugging
    # switch, deliberately independent of ENVIRONMENT so a mislabelled environment can never turn it on.
    db_echo: bool = False

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
    cors_origins: list[str] = ["http://localhost:3000","http://127.0.0.1:3000"]

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

    # Selects the future GST engine (see app/modules/tax/engine.py). Default false. Today the
    # engine only checks readiness and then refuses (HTTP 503 tax_engine_not_implemented): it
    # never produces a tax amount. Takes precedence over TAX_RATE_PERCENT if both are set.
    tax_calculation_enabled: bool = False

    # How long we wait for any single Stripe call. On expiry the outcome is UNKNOWN (the call may
    # have been processed) and is reconciled, never treated as a failure.
    stripe_timeout_seconds: float = Field(default=15.0, gt=0)

    # --- Order/payment lifecycle ---
    # How often the worker looks for unpaid orders past PENDING_ORDER_TIMEOUT_MINUTES. 0 disables the
    # scheduler (then nothing expires orders: only do that for tests/maintenance).
    order_expiry_sweep_interval_seconds: int = Field(default=60, ge=0)
    # Encryption key for provider credentials stored through the admin API (Fernet key: 32 url-safe base64 bytes). Generate with
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Unset = the admin API refuses to store credentials (there is no plaintext fallback). See app/core/secrets.py.
    provider_secrets_key: SecretStr | None = None
    provider_secrets_key_id: str = Field(default="k1", min_length=1, max_length=20, pattern=r"^[A-Za-z0-9_-]+$")
    provider_secrets_previous_keys: str = ""  # "id:key,id:key": old keys kept so existing records still decrypt

    # Webhook events that arrive BEFORE their payment record exists are kept (identifiers only, never the payload) and
    # retried, within bounds: every INTERVAL seconds (0 disables recovery), at most MAX_ATTEMPTS times, and only for WINDOW
    # minutes after the event arrived. After that the event is `expired` and its reconciliation record stays for a human.
    webhook_recovery_interval_seconds: int = Field(default=60, ge=0)
    webhook_recovery_window_minutes: int = Field(default=60, ge=1)
    webhook_recovery_max_attempts: int = Field(default=5, ge=1)

    # After this many failed attempts to learn a refund's outcome it becomes `uncertain` (needs a human).
    refund_max_attempts: int = Field(default=3, ge=1)

    # The currency a NEW store (one with no configured currency and no orders yet) starts with. INR by product
    # decision, but only a default: an administrator can set another currency before the first order, and once a
    # store has one it never changes silently. Must be a currency the platform supports (checked at startup).
    default_store_currency: str = "inr"

    # Currencies this deployment's Stripe account is allowed to charge, comma separated
    # (e.g. "inr" or "usd,eur"). EMPTY by default, so payments are refused until the client
    # confirms in their own Stripe account which currencies it can actually charge. Only
    # currencies verified by this platform (app/core/money.py) are accepted here.
    payment_currencies_enabled: str = ""

    # Live Stripe keys (sk_live_/rk_live_) are refused unless this is true. Keep false: payment
    # currency handling (INR) is not finished. Test keys are unaffected.
    live_payments_enabled: bool = False

    # When true, a product must point at an APPROVED tax classification to be activated
    # or ordered; checkout refuses anything else (HTTP 422, product_tax_unassigned).
    # Default false keeps the pre-GST behaviour. The GST calculation phase turns it on.
    tax_classification_required: bool = False

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
    # --- Rate limits (per client address; counters live in Redis). Login, registration and token refresh keep their fixed limits.
    # Tunable here: the money-moving and abuse-prone routes. A limit is "<count>/<second|minute|hour|day>".
    rate_limit_checkout: str = "10/minute"
    rate_limit_payment: str = "15/minute"
    rate_limit_cart: str = "60/minute"
    # Applied to EVERY limited route (in memory, per process) while Redis is unreachable, so an outage neither turns requests into
    # errors nor switches limiting off completely.
    rate_limit_fallback: str = "30/minute"

    # Largest accepted product/category image upload, in bytes (admin-only endpoints).
    max_image_bytes: int = 5 * 1024 * 1024

    # The interactive API documentation (/docs, /redoc, /openapi.json). None = on in development and testing, OFF in staging and
    # production (it describes the whole admin surface). Set true/false to override.
    api_docs_enabled: bool | None = None

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
    def _environment_and_jwt_algorithm_are_known(self) -> "Settings":
        # An unknown label (e.g. "prod") would silently skip the hardened-environment checks below.
        if self.environment not in ENVIRONMENTS:
            raise ConfigurationError(f"environment must be one of {sorted(ENVIRONMENTS)}")
        if self.jwt_algorithm not in JWT_HMAC_ALGORITHMS:
            raise ConfigurationError(f"jwt_algorithm must be one of {sorted(JWT_HMAC_ALGORITHMS)}")
        return self

    @model_validator(mode="after")
    def _secrets_are_real_in_hardened_environments(self) -> "Settings":
        """Staging/production refuse to start with template or guessable secrets. Messages name the SETTING, never the value.

        A known webhook secret lets anyone who knows a PaymentIntent id (the customer does) forge `payment_intent.succeeded`;
        a known JWT secret lets anyone mint tokens. Development and testing keep their throwaway values."""
        if self.environment not in HARDENED_ENVIRONMENTS:
            return self
        problems: list[str] = []
        if len(self.jwt_secret) < MIN_JWT_SECRET_LENGTH or looks_like_placeholder(self.jwt_secret):
            problems.append(f"JWT_SECRET must be a generated random value of at least {MIN_JWT_SECRET_LENGTH} characters, not a placeholder")
        webhook = self.stripe_webhook_secret
        if not webhook.startswith("whsec_") or len(webhook) < MIN_WEBHOOK_SECRET_LENGTH or looks_like_placeholder(webhook[len("whsec_"):]):
            problems.append("STRIPE_WEBHOOK_SECRET must be the real signing secret from Stripe (whsec_...), not a placeholder")
        key = self.stripe_secret_key
        if not key.startswith(("sk_test_", "sk_live_", "rk_test_", "rk_live_")) or looks_like_placeholder(key.split("_", 2)[-1]):
            problems.append("STRIPE_SECRET_KEY must be a real Stripe key (sk_/rk_ test or live), not a placeholder")
        if problems:
            raise ConfigurationError(f"Refusing to start in '{self.environment}': " + "; ".join(problems))
        return self

    @model_validator(mode="after")
    def _payment_currencies_are_valid(self) -> "Settings":
        from app.core.money import CURRENCIES

        for code in self.enabled_payment_currencies():
            currency = CURRENCIES.get(code)
            if currency is None or not currency.payment_verified:
                raise ValueError(f"payment_currencies_enabled: '{code}' is not a currency verified for payments")
        return self

    @model_validator(mode="after")
    def _provider_secret_keys_are_valid(self) -> "Settings":
        from cryptography.fernet import Fernet

        from app.core.secrets import parse_previous_keys

        keys = [] if self.provider_secrets_key is None else [self.provider_secrets_key.get_secret_value()]
        keys += list(parse_previous_keys(self.provider_secrets_previous_keys).values())
        for key in keys:
            try:
                Fernet(key.encode())
            except Exception as exc:  # the key itself is never included in the message
                raise ValueError("provider secret keys must be valid Fernet keys (32 url-safe base64-encoded bytes)") from exc
        return self

    @model_validator(mode="after")
    def _default_store_currency_is_supported(self) -> "Settings":
        from app.core.money import CURRENCIES

        self.default_store_currency = self.default_store_currency.strip().lower()
        if self.default_store_currency not in CURRENCIES:
            raise ValueError(f"default_store_currency: '{self.default_store_currency}' is not a supported currency")
        return self

    def docs_urls(self) -> dict[str, str | None]:
        on = self.api_docs_enabled if self.api_docs_enabled is not None else self.environment not in HARDENED_ENVIRONMENTS
        return {
            "docs_url": "/docs" if on else None,
            "redoc_url": "/redoc" if on else None,
            "openapi_url": "/openapi.json" if on else None,
        }

    def enabled_payment_currencies(self) -> set[str]:
        return {c.strip().lower() for c in self.payment_currencies_enabled.split(",") if c.strip()}

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
