"""Pixiv infrastructure errors — pure domain/transport exceptions.

- ``PixivError`` — base class for Pixiv client/account errors.
- ``PixivHttpError`` — non-2xx HTTP response with preserved ``status_code``.
- ``RateLimitError`` — the account is rate-limited (429 / Rate Limit body).
- ``AccountInvalidError`` — the account's credentials are permanently bad.
- ``PixivApiError`` — any other API-level failure (retryable unless 4xx
  permanent).
- ``PremiumRequiredError`` — a task needs a premium account and none exists.
"""

from __future__ import annotations

from typing import Any


class PixivError(Exception):
    """Base error for Pixiv HTTP/API failures."""

    def __init__(self, msg: str = "", header: Any = None, body: Any = None) -> None:
        self.header = header
        self.body = body
        super().__init__(msg)


class PixivHttpError(PixivError):
    """A Pixiv HTTP request returned a non-success status code."""

    def __init__(
        self,
        msg: str,
        status_code: int,
        header: Any = None,
        body: Any = None,
    ) -> None:
        self.status_code = status_code
        super().__init__(msg, header=header, body=body)


class RateLimitError(PixivError):
    """Account is rate-limited."""

    def __init__(
        self,
        msg: str = "",
        *,
        retry_after: float | None = None,
        header: Any = None,
        body: Any = None,
    ) -> None:
        self.retry_after = retry_after
        super().__init__(msg, header=header, body=body)


class AccountInvalidError(PixivError):
    """Account token is permanently invalid."""


class PixivApiError(PixivError):
    """Generic Pixiv API error (neither auth-failure nor rate-limit).

    ``status_code`` lets the retry loop distinguish deterministic 4xx failures
    (404/403) from transient errors (401/408/429/5xx).
    """

    def __init__(
        self,
        msg: str,
        status_code: int | None = None,
        header: Any = None,
        body: Any = None,
    ) -> None:
        self.status_code = status_code
        super().__init__(msg, header=header, body=body)


class PremiumRequiredError(PixivError):
    """A task needs a premium account and the pool has none.

    Deliberately NOT a :class:`PixivApiError`, so the client's retry loop
    never retries or silently falls back to a free account.
    """
