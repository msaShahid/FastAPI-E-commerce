import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, field_validator

from app.modules.shipments import validation


class ShipmentCreate(BaseModel):
    """Record a parcel handed to a carrier. Only the carrier is required: a tracking number and link can be added later (PATCH)."""

    model_config = {"extra": "forbid"}

    carrier_name: str
    tracking_number: str | None = None
    tracking_url: str | None = None
    # Also move a PAID order to FULFILLED in the same step (through the normal status rules and history). Default: leave the status alone.
    fulfill_order: bool = False

    @field_validator("carrier_name")
    @classmethod
    def _carrier(cls, v: str) -> str:
        return validation.carrier_name(v)

    @field_validator("tracking_number")
    @classmethod
    def _number(cls, v: str | None) -> str | None:
        return None if v is None else validation.tracking_number(v)

    @field_validator("tracking_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return None if v is None else validation.tracking_url(v)


class ShipmentUpdate(BaseModel):
    """Change any of these. Send `null` for tracking_number or tracking_url to clear it; leave a field out to keep it."""

    model_config = {"extra": "forbid"}

    carrier_name: str | None = None
    tracking_number: str | None = None
    tracking_url: str | None = None
    status: Literal["shipped", "delivered"] | None = None

    @field_validator("carrier_name")
    @classmethod
    def _carrier(cls, v: str | None) -> str | None:
        if v is None:
            raise ValueError("carrier_name cannot be cleared")
        return validation.carrier_name(v)

    @field_validator("tracking_number")
    @classmethod
    def _number(cls, v: str | None) -> str | None:
        return None if v is None else validation.tracking_number(v)

    @field_validator("tracking_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return None if v is None else validation.tracking_url(v)


class ShipmentRead(BaseModel):
    """What a customer sees on their order: the carrier, the tracking number and link, and where the parcel is."""

    id: uuid.UUID
    carrier_name: str
    tracking_number: str | None
    tracking_url: str | None
    status: str
    shipped_at: datetime
    delivered_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}
