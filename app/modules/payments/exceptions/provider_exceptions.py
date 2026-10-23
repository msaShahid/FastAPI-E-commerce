from app.core.exceptions import (
    ConflictError,
    InvalidStateError,
    NotFoundError,
    ServiceUnavailableError,
)


class PaymentProviderUnavailableError(ServiceUnavailableError):
    """A NEW payment cannot be started: the store has no eligible provider. There is NEVER a silent switch to another one."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ProviderConfigNotFoundError(NotFoundError):
    code = "provider_config_not_found"

    def __init__(self) -> None:
        super().__init__("Payment provider configuration not found")


class ProviderConfigConflictError(ConflictError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ProviderConfigInvalidError(InvalidStateError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ProviderNotReadyError(ConflictError):
    """Activation refused: lists the safe reasons (never secrets)."""

    code = "provider_not_ready"

    def __init__(self, blockers: list[str]) -> None:
        super().__init__("This provider cannot be enabled yet: " + ", ".join(blockers))
        self.blockers = blockers
