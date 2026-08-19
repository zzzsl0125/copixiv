"""Domain exceptions — raised by use cases, mapped to HTTP by a FastAPI handler.

Use cases raise these instead of returning ``None`` or importing
``HTTPException``, keeping the application layer decoupled from the
web framework.
"""


class DomainError(Exception):
    """Base for all domain-layer errors.  Carries an HTTP status code
    so the web layer can map it without inspecting the exception type."""

    status_code: int = 500

    def __init__(self, detail: str = ""):
        self.detail = detail
        super().__init__(detail)


class NotFoundError(DomainError):
    """A requested resource was not found."""
    status_code = 404


class NovelNotFoundError(NotFoundError):
    """A novel does not exist / is not fetchable via the webview API.

    Raised by the pixivpy3 ``webview_novel`` patch for *deterministic*
    failures (HTTP 404, empty/deleted content).  Network errors and rate
    limits are NOT this error — they stay retryable upstream.
    """


class ValidationError(DomainError):
    """Input validation failed."""
    status_code = 400


class TaskAlreadyRunningError(DomainError):
    """A task with the same name is already pending or running."""
    status_code = 409
