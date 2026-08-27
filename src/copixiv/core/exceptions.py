"""Domain exceptions — raised by use cases, mapped to HTTP by a FastAPI handler.

Use cases raise these instead of returning ``None`` or importing
``HTTPException``, keeping the application layer decoupled from the
web framework.  The web layer owns the HTTP status mapping (see
``copixiv.app``); these exceptions are pure domain errors and carry no
HTTP status field.
"""


class DomainError(Exception):
    """Base for all domain-layer errors.

    A pure domain exception: it carries no HTTP status.  The web layer
    maps each concrete type to an HTTP status via ``copixiv.app``.
    """

    def __init__(self, detail: str = ""):
        self.detail = detail
        super().__init__(detail)


class NotFoundError(DomainError):
    """A requested resource was not found."""


class NovelNotFoundError(NotFoundError):
    """A novel does not exist / is not fetchable via the webview API.

    Raised by the pixivpy3 ``webview_novel`` patch for *deterministic*
    failures (HTTP 404, empty/deleted content).  Network errors and rate
    limits are NOT this error — they stay retryable upstream.
    """


class ValidationError(DomainError):
    """Input validation failed."""


class TaskAlreadyRunningError(DomainError):
    """A task with the same name is already pending or running."""
