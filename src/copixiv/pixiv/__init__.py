"""Pixiv access module — native async ``httpx`` client and account pool.

Public API:

- ``PixivClient`` (``client.py``) — explicit async API methods, concurrency
  limit, pagination and ``tenacity`` retry.
- ``AccountPool`` (``accounts.py``) — LRU account selection with
  ContextVar-propagated strategy.
- ``PixivAccount`` / ``AccountStrategy`` / ``TokenInfo`` (``account.py``)
  — single-account OAuth state machine and rate-limiting.
- Typed response DTOs in ``models.py`` (``WebviewNovel``, ``PixivNovelInfo``,
  ``NovelListResponse``, ``UserDetailResponse``).
- Exceptions: ``RateLimitError``, ``AccountInvalidError``, ``PixivApiError``,
  ``PixivHttpError``, ``PremiumRequiredError`` (``errors.py``).
"""
