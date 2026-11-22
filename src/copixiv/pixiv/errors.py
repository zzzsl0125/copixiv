"""Pixiv infrastructure errors — pure domain/transport exceptions.

- ``PixivError`` — base class for Pixiv client/account errors.
- ``RateLimitError`` — the account is rate-limited (429 / Rate Limit body).
- ``AccountInvalidError`` — the account's credentials are permanently bad.
- ``PixivApiError`` — any other API-level failure (retryable unless 4xx
  permanent).
- ``PremiumRequiredError`` — a task needs a premium account and none exists.
"""

from __future__ import annotations


class PixivError(Exception):
    """Base error for Pixiv HTTP/API failures."""


class RateLimitError(PixivError):
    """Account is rate-limited."""

    def __init__(
        self,
        msg: str = "",
        *,
        retry_after: float | None = None,
    ) -> None:
        self.retry_after = retry_after
        super().__init__(msg)


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
    ) -> None:
        self.status_code = status_code
        super().__init__(msg)


class PremiumRequiredError(PixivError):
    """A task needs a premium account and the pool has none.

    Deliberately NOT a :class:`PixivApiError`, so the client's retry loop
    never retries or silently falls back to a free account.
    """
