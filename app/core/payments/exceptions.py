from app.core.exceptions import BadRequestError, UpstreamServiceError


class InvalidWebhookSignatureError(BadRequestError):
    def __init__(self) -> None:
        super().__init__("Invalid webhook signature")


class InvalidWebhookPayloadError(BadRequestError):
    """The signature was valid but the event is not shaped like one we understand. Refused (400) without detail."""

    def __init__(self) -> None:
        super().__init__("Invalid webhook payload")


class PaymentProviderError(UpstreamServiceError):
    """Stripe (or whichever provider) itself failed -- rate limited us,
    had an outage, rejected our API key, etc. Not the caller's fault,
    so this must not surface as a generic 500 or a 4xx."""

    public_message = "The payment provider is unavailable. Please try again."

    def __init__(self, message: str = "The payment provider is unavailable") -> None:
        super().__init__(message)


class PaymentProviderTimeoutError(PaymentProviderError):
    """The provider did not answer in time. The request MAY have been processed: the outcome is
    UNKNOWN, never assumed to be a failure. Callers must reconcile before acting."""

    public_message = "The payment provider did not respond in time. Please try again."

    def __init__(self, message: str = "The payment provider did not respond in time") -> None:
        super().__init__(message)
