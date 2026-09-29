from dataclasses import dataclass


@dataclass(frozen=True)
class ShippingAddressInput:
    """
    A shipping address supplied inline at checkout time (as opposed to
    picked by address_id from the caller's saved addresses). Used for
    guest checkout, and for a logged-in user who wants to ship
    somewhere other than one of their saved addresses. Field names
    match Address's columns 1:1, minus user_id/is_default.
    """

    recipient_name: str
    line1: str
    line2: str | None
    city: str
    state: str
    postal_code: str
    country: str

    def as_order_columns(self) -> dict:
        return {
            "shipping_recipient_name": self.recipient_name,
            "shipping_line1": self.line1,
            "shipping_line2": self.line2,
            "shipping_city": self.city,
            "shipping_state": self.state,
            "shipping_postal_code": self.postal_code,
            "shipping_country": self.country,
        }


def address_as_order_columns(address) -> dict:
    """Same snapshot shape as ShippingAddressInput.as_order_columns, but
    from a saved Address model instance (has extra columns we don't
    want to copy, like id/user_id/is_default)."""
    return {
        "shipping_recipient_name": address.recipient_name,
        "shipping_line1": address.line1,
        "shipping_line2": address.line2,
        "shipping_city": address.city,
        "shipping_state": address.state,
        "shipping_postal_code": address.postal_code,
        "shipping_country": address.country,
    }
