"""Domain exceptions — raised by use cases, mapped to HTTP by error_handlers.

Use cases raise these instead of returning ``None`` or importing
``HTTPException``, keeping the application layer decoupled from the
web framework.  The web layer owns the HTTP status mapping (see
``copixiv.error_handlers``); these exceptions are pure domain errors and carry
no HTTP status field.
"""


class DomainError(Exception):
    """Base for all domain-layer errors.

    A pure domain exception: it carries no HTTP status.  The web layer
    maps each concrete type to an HTTP status via ``copixiv.error_handlers``.
    """

    def __init__(self, detail: str = ""):
        self.detail = detail
        super().__init__(detail)


class NotFoundError(DomainError):
    """A requested resource was not found."""


class NovelNotFoundError(NotFoundError):
    """A novel does not exist / is not fetchable via the webview API.

    Raised by the Pixiv client's ``webview_novel`` endpoint for *deterministic*
    failures (HTTP 404, empty/deleted content).  Network errors and rate
    limits are NOT this error — they stay retryable upstream.
    """


class ValidationError(DomainError):
    """Input validation failed."""
