"""Pixiv infrastructure errors — pixivpy3-adjacent exceptions.

These extend ``pixivpy3.PixivError`` so the existing ``except PixivError``
fallback chains keep working, but they carry typed semantics the upper
layers can rely on:

- ``PixivHttpError`` — any non-2xx HTTP response, with the status code
  preserved (raised by the ``requests_call`` monkey patch in ``patch.py``).
- ``RateLimitError`` — the account is rate-limited (429 / Rate Limit body).
- ``AccountInvalidError`` — the account's credentials are permanently bad.
- ``PixivApiError`` — any other API-level failure (retryable by default).
"""

from pixivpy3 import PixivError


class PixivHttpError(PixivError):
    """A Pixiv HTTP request returned a non-success status code.

    ``status_code`` is preserved for status-based classification
    (429 → rate limit, 401 → re-auth, 404 → not found, ...).
    """

    def __init__(self, msg: str, status_code: int, header=None, body=None):
        self.status_code = status_code
        super().__init__(msg, header=header, body=body)


class RateLimitError(PixivError):
    """Account is rate-limited."""


class AccountInvalidError(PixivError):
    """Account token is permanently invalid."""


class PixivApiError(PixivError):
    """Generic Pixiv API error (neither auth-failure nor rate-limit).

    ``account.execute()`` translates raw pixivpy3 ``PixivError`` instances
    into this type so that modules outside the pixivpy3 ACL (see
    docs/MODULARITY.md §2.2) — e.g. ``client.py`` — never import
    pixivpy3 exception types directly.

    ``status_code`` is carried through from :class:`PixivHttpError` so the
    retry loop can tell a *deterministic* failure (404: the work is gone)
    from a transient one.  ``None`` for API-body errors, which carry no HTTP
    status on this path.
    """

    def __init__(self, msg: str, status_code: int | None = None,
                 header=None, body=None):
        self.status_code = status_code
        super().__init__(msg, header=header, body=body)


class PremiumRequiredError(PixivError):
    """A task needs a premium account and the pool has none.

    Deliberately **not** a :class:`PixivApiError`, so the client's retry loop
    does not swallow it: retrying cannot conjure a premium token.  And
    deliberately fatal rather than a silent fallback to a free account —
    ``search_novel(order="popular_desc")`` on a free account yields either an
    error or an unfiltered pile of unrelated works, which the task would then
    record as "success, 0 new novels" while quietly polluting the library.
    """
