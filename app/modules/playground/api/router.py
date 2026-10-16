from fastapi import APIRouter, Depends, Header, HTTPException, Query, status

from app.modules.playground.schemas import ItemCreate, ItemPatch, ItemRead
from app.shared.limits import MAX_PAGE, MAX_PAGE_SIZE

playground_router = APIRouter(prefix="/playground", tags=["playground"])

_fake_items_db: dict[int, ItemRead] = {}
_next_id = 1


@playground_router.get("/items/{item_id}", response_model=ItemRead)
def get_item(item_id: int) -> ItemRead:

    item = _fake_items_db.get(item_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Item {item_id} not found",
        )
    return item


@playground_router.get("/items", response_model=list[ItemRead])
def list_items(
    search: str | None = Query(default=None, max_length=50),
    page: int = Query(default=1, ge=1, le=MAX_PAGE),
    page_size: int = Query(default=10, ge=1, le=MAX_PAGE_SIZE),
) -> list[ItemRead]:

    results = list(_fake_items_db.values())

    if search:
        results = [i for i in results if search.lower() in i.name.lower()]

    start = (page - 1) * page_size
    end = start + page_size
    return results[start:end]


@playground_router.post("/items", response_model=ItemRead, status_code=status.HTTP_201_CREATED)
def create_item(payload: ItemCreate) -> ItemRead:

    global _next_id
    item = ItemRead(id=_next_id, name=payload.name, price_cents=payload.price_cents)
    _fake_items_db[_next_id] = item
    _next_id += 1
    return item


def verify_demo_api_key(x_api_key: str = Header(...)) -> str:

    if x_api_key != "demo-secret-key":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key header",
        )
    return x_api_key


@playground_router.delete("/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_item(item_id: int, api_key: str = Depends(verify_demo_api_key)) -> None:

    if item_id not in _fake_items_db:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")
    del _fake_items_db[item_id]



@playground_router.patch("/items/{item_id}", response_model=ItemRead)
def update_item(item_id: int, payload: ItemPatch) -> ItemRead:
    item = _fake_items_db.get(item_id)

    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Item {item_id} not found",
        )

    updates = payload.model_dump(exclude_unset=True)

    if "name" in updates:
        item.name = updates["name"]

    if "price_cents" in updates:
        item.price_cents = updates["price_cents"]

    return item
