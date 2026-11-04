from app.core.exceptions import ConflictError, InvalidStateError, NotFoundError


class NoDraftError(InvalidStateError):
    code = "site_config_no_draft"

    def __init__(self) -> None:
        super().__init__("There is no draft to publish: save one first")


class ConfigVersionNotFoundError(NotFoundError):
    def __init__(self, version: int) -> None:
        super().__init__("That configuration version does not exist")


class PublishedElsewhereError(ConflictError):
    code = "site_config_changed"

    def __init__(self) -> None:
        super().__init__("The published configuration changed since you loaded it: reload and review before publishing")


class NothingToChangeError(InvalidStateError):
    code = "site_config_unchanged"

    def __init__(self) -> None:
        super().__init__("That is already the published configuration")
