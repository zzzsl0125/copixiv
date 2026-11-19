"""PixivClient — explicit async API methods with tenacity retry and pagination."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import httpx
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception, stop_after_attempt

from copixiv.core.draft import safe_get, safe_set
from copixiv.log import logger

from .account import (
    AccountInvalidError,
    AccountStrategy,
    PixivApiError,
    RateLimitError,
)
from .accounts import AccountPool


def _get_next_url(result: Any) -> str | None:
    """Return ``next_url`` from a Pydantic model or plain dict result."""
    return safe_get(result, "next_url")


def _get_novels(result: Any) -> list:
    """Return the ``novels`` list from a Pydantic model or plain dict result."""
    return safe_get(result, "novels") or []


# 4xx statuses that *are* worth retrying: 401 (expired/revoked access token —
# ``PixivAccount.execute`` resets auth state so the next attempt re-auths),
# 408 (request timeout), and 429 (rate limited — handled via cooldown).
_TRANSIENT_4XX: frozenset[int] = frozenset({401, 408, 429})


def _is_permanent(exc: PixivApiError) -> bool:
    """True when retrying *exc* cannot change the outcome."""
    status = exc.status_code
    if status is None:
        return False
    return 400 <= status < 500 and status not in _TRANSIENT_4XX


def _is_retryable_exc(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (AccountInvalidError, RateLimitError, httpx.HTTPError, ConnectionError),
    ):
        return True
    if isinstance(exc, PixivApiError):
        return not _is_permanent(exc)
    return False


class PixivClient:
    """Pixiv API client with explicit methods, account pooling, and pagination."""

    def __init__(
        self,
        account_pool: AccountPool,
        max_concurrency: int = 5,
    ):
        self.pool = account_pool
        self.max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)

    @asynccontextmanager
    async def account_rule(
        self,
        need_premium: bool = False,
        force_account: str | None = None,
        force_follow: bool = False,
    ):
        """Temporarily override account selection strategy."""
        token = self.pool.set_strategy(
            AccountStrategy(
                need_premium=need_premium,
                force_account=force_account,
                force_follow=force_follow,
            )
        )
        try:
            yield self
        finally:
            self.pool.reset_strategy(token)

    # ---- Public API methods ------------------------------------------------

    async def webview_novel(self, novel_id: int) -> Any:
        """Fetch a single novel with full text content."""
        return await self._call("webview_novel", novel_id)

    async def user_novels(
        self,
        author_id: int,
        fetch_all: bool = False,
    ) -> Any:
        """Fetch all novels by an author."""
        return await self._call(
            "user_novels",
            author_id,
            fetch_all=fetch_all,
        )

    async def user_detail(self, user_id: int) -> Any:
        """Fetch user profile details."""
        return await self._call("user_detail", user_id)

    async def user_follow_add(self, user_id: int) -> Any:
        """Follow a user."""
        return await self._call("user_follow_add", user_id)

    async def user_follow_delete(self, user_id: int) -> Any:
        """Unfollow a user."""
        return await self._call("user_follow_delete", user_id)

    async def novel_follow(self, fetch_til: datetime | None = None) -> Any:
        """Fetch novels from followed users."""
        return await self._call("novel_follow", fetch_til=fetch_til)

    async def novel_ranking(
        self,
        mode: str = "day_r18",
        date: Any = None,
        fetch_all: bool = False,
    ) -> Any:
        """Fetch novel rankings."""
        return await self._call(
            "novel_ranking",
            mode=mode,
            date=date,
            fetch_all=fetch_all,
        )

    async def search_novel(
        self,
        keyword: str,
        search_type: str = "keyword",
        order: str = "popular_desc",
        start_date: Any = None,
        end_date: Any = None,
        fetch_minlike: int | None = None,
    ) -> Any:
        """Search novels by keyword."""
        return await self._call(
            "search_novel",
            keyword,
            search_target=search_type or "partial_match_for_tags",
            sort=order or "date_desc",
            start_date=start_date,
            end_date=end_date,
            fetch_minlike=fetch_minlike,
        )

    async def novel_series(
        self,
        series_id: int,
        fetch_all: bool = False,
    ) -> Any:
        """Fetch all novels in a series."""
        return await self._call("novel_series", series_id, fetch_all=fetch_all)

    # ---- Internal call machinery -------------------------------------------

    _MAX_RETRIES: int = 3
    _BACKOFF_BASE: float = 1.0  # seconds — doubled each attempt
    _BACKOFF_CAP: float = 10.0  # seconds
    _MAX_PAGES: int = 200

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential backoff: 1s, 2s, 4s, capped at 10s."""
        return min(
            PixivClient._BACKOFF_BASE * (2**attempt),
            PixivClient._BACKOFF_CAP,
        )

    def _wait_seconds(self, retry_state: RetryCallState) -> float:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if isinstance(exc, AccountInvalidError):
            return 0.0
        if isinstance(exc, RateLimitError):
            has_ready = any(a.valid and not a.in_cooldown for a in self.pool.accounts)
            if has_ready:
                return 0.0
            if exc.retry_after is not None:
                return min(max(exc.retry_after, 0.0), self._BACKOFF_CAP)
        attempt = max(retry_state.attempt_number - 1, 0)
        return self._backoff(attempt)

    async def _execute_with_retry(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Call ``account.execute()`` with ``tenacity`` backoff and account rotation."""
        total_attempts = self._MAX_RETRIES + 1
        current_account: list[Any] = [None]

        def _log_before_sleep(retry_state: RetryCallState) -> None:
            exc = retry_state.outcome.exception() if retry_state.outcome else None
            account = current_account[0]
            attempt_no = retry_state.attempt_number
            if isinstance(exc, AccountInvalidError):
                logger.warning(
                    f"Account {account} is invalid, switching (attempt {attempt_no}/{total_attempts})"
                )
            elif isinstance(exc, RateLimitError):
                logger.warning(f"Rate limited on {account}, retry {attempt_no}/{total_attempts}")
            else:
                logger.warning(f"API error on {account}: {exc}, retry {attempt_no}/{total_attempts}")

        async def _async_sleep(seconds: float) -> None:
            if seconds > 0:
                await asyncio.sleep(seconds)

        retrying = AsyncRetrying(
            stop=stop_after_attempt(total_attempts),
            retry=retry_if_exception(_is_retryable_exc),
            wait=self._wait_seconds,
            before_sleep=_log_before_sleep,
            sleep=_async_sleep,
            reraise=True,
        )

        async for attempt in retrying:
            with attempt:
                account = self.pool.select()
                current_account[0] = account
                try:
                    return await account.execute(method, *args, **kwargs)
                except PixivApiError as exc:
                    if _is_permanent(exc):
                        logger.warning(
                            f"Permanent API error on {account} ({exc.status_code}): {exc} — not retrying"
                        )
                    raise

        raise AssertionError("unreachable")

    async def _call(
        self,
        method: str,
        *args: Any,
        fetch_all: bool = False,
        fetch_til: datetime | None = None,
        fetch_minlike: int | None = None,
        **kwargs: Any,
    ) -> Any:
        """Execute an API call with optional pagination."""
        async with self._semaphore:
            result = await self._execute_with_retry(method, *args, **kwargs)

        if result is None:
            return None

        if not (fetch_all or fetch_til or fetch_minlike):
            return result

        return await self._paginate(method, result, fetch_til, fetch_minlike)

    async def _paginate(
        self,
        method: str,
        result: Any,
        fetch_til: datetime | None,
        fetch_minlike: int | None,
    ) -> Any:
        """Follow ``next_url`` across pages, collecting results."""
        page = 1

        novels_count = len(_get_novels(result))
        logger.info(f"Paginate {method}: page {page} — {novels_count} items")

        next_url = _get_next_url(result)
        while next_url:
            page += 1
            if page > self._MAX_PAGES:
                logger.warning(
                    f"Paginate {method}: exceeded {self._MAX_PAGES} pages, stopping",
                )
                break

            async with self._semaphore:
                next_result = await self._execute_with_retry("get_next_page", next_url)

            next_novels = _get_novels(next_result)
            new_next_url = _get_next_url(next_result)

            safe_set(result, "novels", _get_novels(result) + next_novels)
            safe_set(result, "next_url", new_next_url)

            novels_count += len(next_novels)
            logger.info(
                f"Paginate {method}: page {page} — {len(next_novels)} items (total: {novels_count})",
            )

            next_url = new_next_url

            if next_novels and self._should_stop(next_novels[-1], fetch_til, fetch_minlike):
                logger.info(
                    f"Paginate {method}: stopping — threshold reached",
                )
                break

        logger.info(
            f"Paginate {method}: done — {page} pages, {novels_count} items total",
        )
        return result

    @staticmethod
    def _should_stop(
        last_item: Any,
        fetch_til: datetime | None,
        fetch_minlike: int | None,
    ) -> bool:
        """Determine if pagination should stop based on date or bookmark thresholds."""
        if fetch_til:
            raw_date = safe_get(last_item, "create_date")
            if raw_date is not None:
                try:
                    create_dt = (
                        raw_date
                        if isinstance(raw_date, datetime)
                        else datetime.fromisoformat(str(raw_date).strip())
                    )
                    if create_dt.tzinfo is not None and fetch_til.tzinfo is None:
                        create_dt = create_dt.replace(tzinfo=None)
                    elif create_dt.tzinfo is None and fetch_til.tzinfo is not None:
                        fetch_til = fetch_til.replace(tzinfo=None)
                    if create_dt < fetch_til:
                        return True
                except Exception:
                    return False
        if fetch_minlike:
            bookmarks = safe_get(last_item, "total_bookmarks")
            if bookmarks is not None and bookmarks < fetch_minlike:
                return True
        return False
