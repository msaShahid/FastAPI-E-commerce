import re
import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, SecretStr, field_validator

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class EmailProviderSave(BaseModel):
    """Create or update one provider's settings. On update, leave `username`/`password` out to keep the stored values."""

    model_config = {"extra": "forbid"}  # in particular: no `host` or `port` field can be smuggled in

    mode: str | None = Field(default=None, max_length=20)
    username: str | None = Field(default=None, max_length=254)
    password: SecretStr | None = Field(default=None, max_length=255)
    from_email: EmailStr
    from_name: str = Field(min_length=1, max_length=100)

    @field_validator("from_name", "username", "mode")
    @classmethod
    def _no_control_characters(cls, value: str | None) -> str | None:
        if value is not None and _CONTROL.search(value):
            raise ValueError("must not contain line breaks or control characters")
        return value.strip() if isinstance(value, str) else value


class FieldSpec(BaseModel):
    name: str
    label: str
    secret: bool = False
    required: bool = True


class ProviderCatalogEntry(BaseModel):
    provider: str
    label: str
    mode_label: str | None
    modes: dict[str, str] | None             # mode -> description (null: this provider has no modes)
    fields: list[FieldSpec]
    host_by_mode: dict[str, str]             # shown for transparency: the host is fixed, not editable
    notes: list[str]


class ProviderConfigRead(BaseModel):
    """What the admin UI may see. No credential, ciphertext or host override ever appears here."""

    provider: str
    label: str
    configured: bool
    mode: str | None = None
    username_hint: str | None = None
    password_set: bool = False
    password_hint: str | None = None
    from_email: str | None = None
    from_name: str | None = None
    is_active: bool = False
    delivers_to_real_recipients: bool | None = None
    last_test_at: datetime | None = None
    last_test_ok: bool | None = None
    last_test_error: str | None = None
    last_sent_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error_code: str | None = None
    warnings: list[str] = []
    id: uuid.UUID | None = None


class EffectiveEmail(BaseModel):
    source: str                              # "database" (an admin-selected service) | "environment"
    provider: str | None
    label: str
    delivers_to_real_recipients: bool


class EmailSettingsRead(BaseModel):
    effective: EffectiveEmail
    providers: list[ProviderConfigRead]


class ProbeResult(BaseModel):
    ok: bool
    error_code: str | None = None
    message: str
