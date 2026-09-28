from app.core.exceptions import BadRequestError, UpstreamServiceError


class InvalidWebhookSignatureError(BadRequestError):
    def __init__(self) -> None:
        super().__init__("Invalid webhook signature")


class PaymentProviderError(UpstreamServiceError):
    """Stripe (or whichever provider) itself failed -- rate limited us,
    had an outage, rejected our API key, etc. Not the caller's fault,
    so this must not surface as a generic 500 or a 4xx."""

    def __init__(self, message: str = "The payment provider is unavailable") -> None:
        super().__init__(message)
