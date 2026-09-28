from datetime import datetime

from pydantic import BaseModel, Field


class CategoryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)



class CategoryUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=2000)
    is_active: bool | None = None


class CategoryRead(BaseModel):
    id: int
    name: str
    slug: str
    description: str | None
    image_path: str | None
    image_url: str | None = None
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}
