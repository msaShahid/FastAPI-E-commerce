import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")   # keeps newlines and tabs in a review body, drops other control characters


def _clean(value: str | None, *, single_line: bool) -> str | None:
    if value is None:
        return None
    value = _CONTROL.sub("", value)
    value = re.sub(r"\s+", " ", value) if single_line else re.sub(r"[ \t]+", " ", value)
    value = value.strip()
    return value or None


class ReviewCreate(BaseModel):
    """Plain text only: a client must escape it when it shows it. Empty text counts as none."""

    model_config = {"extra": "forbid"}

    rating: int = Field(ge=1, le=5)
    title: str | None = Field(default=None, max_length=100)
    body: str | None = Field(default=None, max_length=2000)

    @field_validator("title")
    @classmethod
    def _title(cls, v):
        return _clean(v, single_line=True)

    @field_validator("body")
    @classmethod
    def _body(cls, v):
        return _clean(v, single_line=False)


class ReviewUpdate(BaseModel):
    """Change any of these; send `null` for title or body to remove it. Fields left out stay as they are."""

    model_config = {"extra": "forbid"}

    rating: int | None = Field(default=None, ge=1, le=5)
    title: str | None = Field(default=None, max_length=100)
    body: str | None = Field(default=None, max_length=2000)

    _title = field_validator("title")(lambda cls, v: _clean(v, single_line=True))
    _body = field_validator("body")(lambda cls, v: _clean(v, single_line=False))


class ReviewRead(BaseModel):
    id: int
    product_id: int
    rating: int
    title: str | None
    body: str | None
    display_name: str                 # a masked form of the customer's username: never an email address
    verified_purchase: bool = True    # only customers who bought the product can review it
    created_at: datetime
    updated_at: datetime
    status: str | None = None         # shown to the author and to administrators only


class ReviewSummary(BaseModel):
    product_id: int
    count: int
    average: float | None             # one decimal; null when there are no reviews
    distribution: dict[int, int]      # stars -> number of reviews, always 1 to 5


class ReviewAdminRead(ReviewRead):
    user_id: str
    hidden_reason: str | None = None


class ReviewModeration(BaseModel):
    model_config = {"extra": "forbid"}

    status: Literal["published", "hidden"]
    reason: str | None = Field(default=None, max_length=200)
