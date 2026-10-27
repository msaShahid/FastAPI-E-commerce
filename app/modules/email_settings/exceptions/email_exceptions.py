from app.core.exceptions import ConflictError, InvalidStateError, NotFoundError


class EmailProviderNotFoundError(NotFoundError):
    code = "email_provider_not_configured"

    def __init__(self, provider: str) -> None:
        super().__init__(f"{provider} has not been configured yet")


class EmailProviderActiveError(ConflictError):
    code = "email_provider_active"

    def __init__(self) -> None:
        super().__init__("This email service is the active one. Switch to another service (or deactivate) before deleting it.")


class InvalidEmailProviderSettingsError(InvalidStateError):
    code = "invalid_email_provider_settings"


class EmailProviderTestFailedError(InvalidStateError):
    """The provider's connection test failed. Carries only a fixed error code and its fixed explanation, never server text."""

    code = "email_provider_test_failed"

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)
