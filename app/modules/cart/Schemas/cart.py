from pydantic import BaseModel, Field


class CartItemCreate(BaseModel):
    product_id: int
    quantity: int = Field(default=1, gt=0)


class CartItemUpdate(BaseModel):
    quantity: int = Field(gt=0)


class CartItemProductSummary(BaseModel):
    id: int
    name: str
    slug: str
    price_cents: int 

    model_config = {"from_attributes": True}


class CartItemRead(BaseModel):
    id: int
    product: CartItemProductSummary
    quantity: int
    price_cents_snapshot: int
    price_changed: bool  
    line_total_cents: int  


class CartRead(BaseModel):
    items: list[CartItemRead]
    subtotal_cents: int
    item_count: int