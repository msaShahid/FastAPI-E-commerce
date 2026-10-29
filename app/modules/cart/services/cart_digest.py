"""A fingerprint of what is in a cart: which products and variants, how many of each. NOT the prices (a price change is handled by the
price_changed flow). Order of lines does not matter. Returned with the cart so a client can show it was the cart they reviewed, and used
by checkout to notice that a replayed idempotency key now comes with a different cart."""

import hashlib


def cart_digest(lines) -> str:
    """`lines`: anything with product_id, variant_id and quantity."""
    parts = sorted(f"{line.product_id}:{line.variant_id or 0}:{line.quantity}" for line in lines)
    return hashlib.sha256("|".join(parts).encode()).hexdigest()
