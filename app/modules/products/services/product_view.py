"""What a shopper (or an administrator) is shown about a product's price and availability. Pure functions: no database access.

Effective price = the variant's override if it has one, otherwise the product's base price. For a product with options:
  * price_from / price_to are the lowest and highest effective price over its ACTIVE variants, in stock or not;
  * availability is that of its BEST active variant (in_stock beats low_stock beats out_of_stock): a shopper can buy at least that;
  * exact stock is the sum over its variants (all of them for an administrator, active ones for a public "exact" setting).
Exact stock is shown only to administrators, or to everyone if PUBLIC_STOCK_VISIBILITY=exact."""

from sqlalchemy import inspect as sa_inspect

from app.modules.products.schemas.product import OptionRead, VariantOptionRead, VariantRead

_RANK = {"out_of_stock": 0, "low_stock": 1, "in_stock": 2}


def availability(stock: int, threshold: int) -> str:
    if stock <= 0:
        return "out_of_stock"
    return "low_stock" if stock <= threshold else "in_stock"


def effective_price(variant, base_price: int) -> int:
    return variant.price_override_cents if variant.price_override_cents is not None else base_price


def _attached(product, name: str):
    try:
        unloaded = sa_inspect(product).unloaded
    except Exception:  # noqa: BLE001 - not a mapped object (a plain stand-in): nothing can be lazily loaded
        unloaded = set()
    if name in unloaded:
        raise RuntimeError(f"product {product.id} has options but its {name} were not attached (call ProductService.attach_variant_views)")
    return getattr(product, name)


def _value_labels(product) -> dict[int, tuple[str, str]]:
    labels: dict[int, tuple[str, str]] = {}
    for option in _attached(product, "option_rows"):
        for value in option.values:
            labels[value.id] = (option.name, value.value)
    return labels


def view_fields(product, *, is_admin: bool, detail: bool, settings) -> dict:
    """The price, availability and (for detail) variant fields to merge into a ProductRead."""
    threshold = settings.low_stock_display_threshold
    show_exact = is_admin or settings.public_stock_visibility == "exact"

    if product.option_count == 0:
        fields = {
            "has_variants": False, "price_cents": product.price_cents, "price_from_cents": product.price_cents,
            "price_to_cents": product.price_cents, "has_price_range": False, "variant_count": 0,
            "available": product.stock > 0, "availability": availability(product.stock, threshold),
            "stock": product.stock if show_exact else None,
        }
        if is_admin:
            fields["base_price_cents"] = product.price_cents
        return fields

    variants = _attached(product, "variant_rows")
    active = [v for v in variants if v.status == "active"]
    prices = [effective_price(v, product.price_cents) for v in active] or [product.price_cents]
    best = max((availability(v.stock, threshold) for v in active), key=_RANK.__getitem__, default="out_of_stock")
    counted = variants if is_admin else active
    fields = {
        "has_variants": True, "price_cents": min(prices), "price_from_cents": min(prices), "price_to_cents": max(prices),
        "has_price_range": min(prices) != max(prices), "variant_count": len(counted),
        "available": best != "out_of_stock", "availability": best,
        "stock": sum(v.stock for v in counted) if show_exact else None,
    }
    if is_admin:
        fields["base_price_cents"] = product.price_cents
    if detail:
        labels = _value_labels(product)
        fields["options"] = [OptionRead(id=o.id, name=o.name, values=[v.value for v in o.values], value_ids=[v.id for v in o.values]) for o in _attached(product, "option_rows")]
        fields["variants"] = [
            VariantRead(
                id=v.id, sku=v.sku,
                options=[VariantOptionRead(name=labels[i][0], value=labels[i][1])
                         for i in (v.value1_id, v.value2_id, v.value3_id)[: product.option_count] if i in labels],
                price_cents=effective_price(v, product.price_cents), price_overridden=v.price_override_cents is not None,
                available=v.stock > 0 and v.status == "active", availability=availability(v.stock, threshold),
                stock=v.stock if show_exact else None, status=v.status if is_admin else None,
            )
            for v in counted
        ]
    return fields
