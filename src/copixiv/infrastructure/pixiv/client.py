"""PixivClient — explicit API methods, no __getattr__ magic.

Each API method is a regular async method with explicit parameters.
Pagination, handler dispatch, and rate limiting are composed through the
AccountPool and RequestManager.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime

from dateutil import parser as date_parser

from copixiv.domain.services.parsing import safe_get
from .account import AccountStrategy, RateLimitError, AccountInvalidError
from .accounts import AccountPool

from copixiv.app.logger import logger


class PixivClient:
    """Pixiv API client with explicit methods, account pooling, and pagination.

    Usage::

        client = PixivClient(account_pool)

        async with client.account_rule(need_premium=True):
            result = await client.search_novel("R-18", fetch_minlike=500)

        novel = await client.webview_novel(12345678)
    """

    def __init__(
        self,
        account_pool: AccountPool,
        max_concurrency: int = 5,
        min_interval: float = 2.0,
    ):
        self.pool = account_pool
        self.max_concurrency = max_concurrency
        self.min_interval = min_interval
        self._semaphore = asyncio.Semaphore(max_concurrency)

    @asynccontextmanager
    async def account_rule(
        self,
        need_premium: bool = False,
        force_account: str | None = None,
    ):
        """Temporarily override account selection strategy."""
        token = self.pool.set_strategy(
            AccountStrategy(need_premium=need_premium, force_account=force_account)
        )
        try:
            yield self
        finally:
            self.pool.reset_strategy(token)

    # ---- Public API methods ------------------------------------------------

    async def webview_novel(self, novel_id: int) -> dict | None:
        """Fetch a single novel with full text content."""
        return await self._call("webview_novel", novel_id)

    async def user_novels(
        self, author_id: int, fetch_all: bool = False, handler=None,
    ) -> dict:
        """Fetch all novels by an author."""
        return await self._call(
            "user_novels", author_id, fetch_all=fetch_all, handler=handler,
        )

    async def user_detail(self, user_id: int) -> dict:
        """Fetch user profile details."""
        return await self._call("user_detail", user_id)

    async def user_follow_add(self, user_id: int) -> dict:
        """Follow a user."""
        return await self._call("user_follow_add", user_id)

    async def user_follow_delete(self, user_id: int) -> dict:
        """Unfollow a user."""
        return await self._call("user_follow_delete", user_id)

    async def novel_follow(self, fetch_til=None, handler=None) -> dict:
        """Fetch novels from followed users."""
        return await self._call(
            "novel_follow", fetch_til=fetch_til, handler=handler,
        )

    async def novel_ranking(
        self,
        mode: str = "day_r18",
        date=None,
        fetch_all: bool = False,
        handler=None,
    ) -> dict:
        """Fetch novel rankings."""
        return await self._call(
            "novel_ranking", mode=mode, date=date, fetch_all=fetch_all,
            handler=handler,
        )

    async def search_novel(
        self,
        keyword: str,
        search_type: str = "keyword",
        order: str = "popular_desc",
        start_date=None,
        end_date=None,
        fetch_minlike: int | None = None,
        handler=None,
    ) -> dict:
        """Search novels by keyword."""
        return await self._call(
            "search_novel",
            keyword,
            search_type=search_type,
            order=order,
            start_date=start_date,
            end_date=end_date,
            fetch_minlike=fetch_minlike,
            handler=handler,
        )

    async def novel_series(
        self, series_id: int, fetch_all: bool = False
    ) -> dict:
        """Fetch all novels in a series."""
        return await self._call(
            "novel_series", series_id, fetch_all=fetch_all
        )

    # ---- Internal call machinery -------------------------------------------

    # Retry configuration
    _MAX_RETRIES: int = 3
    _BACKOFF_BASE: float = 1.0     # seconds — doubled each attempt
    _BACKOFF_CAP: float = 10.0     # seconds

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential backoff: 1s, 2s, 4s, capped at 10s."""
        return min(PixivClient._BACKOFF_BASE * (2 ** attempt),
                   PixivClient._BACKOFF_CAP)

    async def _execute_with_retry(self, method: str, *args, **kwargs):
        """Call ``account.execute()`` with exponential backoff and
        automatic account switching on rate-limit or invalid-account errors.

        ``start_cooldown()`` / account invalidation is handled inside
        ``account.execute()`` — this method only manages the retry loop.
        """
        last_error: Exception | None = None

        for attempt in range(self._MAX_RETRIES + 1):
            account = self.pool.select()
            try:
                return await account.execute(method, *args, **kwargs)
            except AccountInvalidError:
                # Account already marked INVALID by authenticate();
                # select() will skip it on the next iteration.
                logger.warning(
                    f"Account {account} is invalid, switching "
                    f"(attempt {attempt + 1}/{self._MAX_RETRIES + 1})"
                )
                last_error = AccountInvalidError(str(account))
            except RateLimitError:
                # Cooldown already started in account.execute().
                # TODO: parse Retry-After from the HTTP response if
                # pixivpy3 ever exposes it, rather than using the
                # fixed cooling_duration.
                logger.warning(
                    f"Rate limited on {account}, "
                    f"retry {attempt + 1}/{self._MAX_RETRIES + 1}"
                )
                last_error = RateLimitError(str(account))
                if attempt < self._MAX_RETRIES:
                    await asyncio.sleep(self._backoff(attempt))
            except Exception as e:
                logger.warning(
                    f"API error on {account}: {e}, "
                    f"retry {attempt + 1}/{self._MAX_RETRIES + 1}"
                )
                last_error = e
                if attempt < self._MAX_RETRIES:
                    await asyncio.sleep(self._backoff(attempt))

        raise last_error  # type: ignore[misc]

    async def _call(self, method: str, *args, **kwargs):
        """Execute an API call with optional pagination and handler dispatch."""
        fetch_all = kwargs.pop("fetch_all", None)
        fetch_til = kwargs.pop("fetch_til", None)
        fetch_minlike = kwargs.pop("fetch_minlike", None)
        handler = kwargs.pop("handler", None)

        async with self._semaphore:
            result = await self._execute_with_retry(method, *args, **kwargs)

        if result is None:
            return None

        if not (fetch_all or fetch_til or fetch_minlike):
            return await self._run_handlers(result, handler)

        return await self._paginate(
            method, result, handler, fetch_til, fetch_minlike
        )

    async def _run_handlers(self, result, handler):
        """Run a handler coroutine on the result, attaching output."""
        result.handler_results = []
        if handler is not None:
            tasks = [asyncio.create_task(handler(result))]
            flat = await asyncio.gather(*tasks)
            result.handler_results = [
                item for sublist in flat for item in (sublist or [])
            ]
        return result

    async def _paginate(
        self, method, result, handler, fetch_til, fetch_minlike
    ):
        """Follow ``next_url`` across pages, collecting results."""
        handler_tasks: list[asyncio.Task] = []
        page = 1

        if handler:
            handler_tasks.append(asyncio.create_task(handler(result)))

        novels_count = len(safe_get(result, "novels", []))
        logger.info(f"Paginate {method}: page {page} — {novels_count} items")

        while result.next_url:
            page += 1
            account = self.pool.select()
            next_qs = account.api.parse_qs(result.next_url)

            async with self._semaphore:
                next_result = await self._execute_with_retry(
                    method, **next_qs
                )

            result.next_url = safe_get(next_result, "next_url")

            if handler:
                handler_tasks.append(asyncio.create_task(handler(next_result)))
            else:
                result.novels += safe_get(next_result, "novels", [])

            next_novels = safe_get(next_result, "novels", [])
            novels_count += len(next_novels)
            logger.info(
                f"Paginate {method}: page {page} — "
                f"{len(next_novels)} items (total: {novels_count})",
            )

            if next_novels and self._should_stop(
                next_novels[-1], fetch_til, fetch_minlike
            ):
                logger.info(
                    f"Paginate {method}: stopping — threshold reached",
                )
                break

        logger.info(
            f"Paginate {method}: done — {page} pages, {novels_count} items total",
        )

        if handler_tasks:
            flat = await asyncio.gather(*handler_tasks)
            result.handler_results = [
                item for sublist in flat for item in (sublist or [])
            ]
        else:
            result.handler_results = []

        return result

    @staticmethod
    def _should_stop(last_item, fetch_til, fetch_minlike) -> bool:
        """Determine if pagination should stop based on date or bookmark thresholds."""
        if fetch_til and "create_date" in last_item:
            if date_parser.parse(last_item["create_date"]) < fetch_til:
                return True
        if fetch_minlike and "total_bookmarks" in last_item:
            if last_item["total_bookmarks"] < fetch_minlike:
                return True
        return False
