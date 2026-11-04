"""How a cart line is shown: the variant chosen, its CURRENT effective price, and whether it can still be bought."""

from app.modules.cart.Schemas.cart import (
    CartItemProductSummary,
    CartItemRead,
    CartItemVariantSummary,
    CartRead,
)
from app.modules.cart.services.cart_digest import cart_digest
from app.modules.products.schemas.product import VariantOptionRead
from app.modules.products.services.product_view import effective_price
from app.shared.enums.product_status import ProductStatus


def line_state(item) -> tuple[int, int, str | None]:
    """(current effective price, units in stock for this line, reason it cannot be bought now or None)."""
    product, variant = item.product, getattr(item, "variant_obj", None)
    if variant is not None:
        price, stock = effective_price(variant, product.price_cents), variant.stock
        if product.status != ProductStatus.ACTIVE or variant.status != "active":
            return price, stock, "unavailable"
    else:
        price, stock = product.price_cents, product.stock
        if product.status != ProductStatus.ACTIVE:
            return price, stock, "unavailable"
    if stock <= 0:
        return price, stock, "out_of_stock"
    if stock < item.quantity:
        return price, stock, "insufficient_stock"
    return price, stock, None


def _product_summary(product, storage) -> CartItemProductSummary:
    image_path = getattr(product, "image_path", None)
    return CartItemProductSummary(
        id=product.id, name=product.name, slug=product.slug, price_cents=product.price_cents,
        image_url=storage.get_url(path=image_path) if storage is not None and image_path else None,
    )


def build_cart_read(cart, storage=None) -> CartRead:
    """`storage` turns a product's stored image path into the URL the catalogue shows; without it lines carry no image."""
    items, subtotal = [], 0
    for item in cart.items:
        current_price, _stock, reason = line_state(item)
        line_total = item.quantity * item.price_cents_snapshot
        subtotal += line_total
        variant = getattr(item, "variant_obj", None)
        items.append(CartItemRead(
            id=item.id, product=_product_summary(item.product, storage), quantity=item.quantity, price_cents_snapshot=item.price_cents_snapshot,
            price_changed=item.price_cents_snapshot != current_price, line_total_cents=line_total,
            current_price_cents=current_price, available=reason is None, unavailable_reason=reason,
            variant=None if variant is None else CartItemVariantSummary(
                id=variant.id, sku=variant.sku,
                options=[VariantOptionRead(name=n, value=v) for n, v in getattr(item, "variant_labels", [])],
            ),
        ))
    return CartRead(items=items, subtotal_cents=subtotal, item_count=len(items), cart_digest=cart_digest(cart.items))
