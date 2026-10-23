import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.shared.mixins import TimestampMixin


class PaymentProviderConfig(Base, TimestampMixin):
    """One payment provider configured for THIS store (a NovaCart deployment serves exactly one store, so there is no store key:
    isolation between clients is deployment isolation, and no API takes a store identifier).

    Credentials are stored ENCRYPTED (app/core/secrets.py): only ciphertext and a non-secret hint exist here. Disabling a
    configuration blocks NEW payments only: it is never deleted, because existing payments, refunds and webhooks are reconciled
    with it. Database constraints keep the state consistent: one config per (provider, mode); at most one preferred, and only an
    enabled one; enabled requires valid credentials; live-enabled additionally requires a verified webhook and recorded
    sandbox-verification and production-review attestations.
    """

    __tablename__ = "payment_provider_configs"
    __table_args__ = (
        UniqueConstraint("provider", "mode", name="uq_provider_configs_provider_mode"),
        Index("ux_provider_configs_one_preferred", "is_preferred", unique=True, postgresql_where=text("is_preferred")),
        CheckConstraint("provider ~ '^[a-z][a-z0-9_]{1,29}$'", name="ck_provider_configs_provider"),
        CheckConstraint("mode IN ('test', 'live')", name="ck_provider_configs_mode"),
        CheckConstraint("status IN ('draft', 'enabled', 'disabled')", name="ck_provider_configs_status"),
        CheckConstraint("credentials_status IN ('unvalidated', 'valid', 'invalid')", name="ck_provider_configs_cred_status"),
        CheckConstraint("webhook_status IN ('unverified', 'verified')", name="ck_provider_configs_webhook_status"),
        CheckConstraint("NOT is_preferred OR status = 'enabled'", name="ck_provider_configs_preferred_enabled"),
        CheckConstraint(
            "status <> 'enabled' OR (credentials_status = 'valid' AND credentials_ciphertext IS NOT NULL)",
            name="ck_provider_configs_enabled_valid",
        ),
        CheckConstraint(
            "NOT (mode = 'live' AND status = 'enabled') OR (webhook_status = 'verified' "
            "AND sandbox_verified_at IS NOT NULL AND production_review_at IS NOT NULL)",
            name="ck_provider_configs_live_requirements",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(30))
    mode: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20), default="draft", server_default="draft")
    is_preferred: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    credentials_ciphertext: Mapped[str | None] = mapped_column(Text, default=None)
    credential_hint: Mapped[str | None] = mapped_column(String(20), default=None)   # "...abcd": never enough to use the secret
    public_identifier: Mapped[str | None] = mapped_column(String(100), default=None)  # a PUBLIC id (e.g. Razorpay key id), not a secret
    credentials_status: Mapped[str] = mapped_column(String(20), default="unvalidated", server_default="unvalidated")
    credentials_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    webhook_secret_ciphertext: Mapped[str | None] = mapped_column(Text, default=None)
    previous_webhook_secret_ciphertext: Mapped[str | None] = mapped_column(Text, default=None)  # rotation overlap, until retired
    webhook_hint: Mapped[str | None] = mapped_column(String(20), default=None)
    webhook_status: Mapped[str] = mapped_column(String(20), default="unverified", server_default="unverified")
    webhook_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    currencies: Mapped[list] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    sandbox_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    sandbox_verification_ref: Mapped[str | None] = mapped_column(Text, default=None)
    production_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    production_review_ref: Mapped[str | None] = mapped_column(Text, default=None)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")  # +1 on every credential change/rotation
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), default=None)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), default=None)
