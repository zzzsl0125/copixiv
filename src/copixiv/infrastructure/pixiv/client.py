"""PixivClient — explicit API methods, no __getattr__ magic.

Each API method is a regular async method with explicit parameters.
Pagination and rate limiting are composed through the AccountPool and
RequestManager.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from dateutil import parser as date_parser
from requests.exceptions import RequestException

from copixiv.domain.services.parsing import safe_get, safe_set

from .account import (
    AccountStrategy,
    RateLimitError,
    AccountInvalidError,
    PixivApiError,
)
from .accounts import AccountPool

from copixiv.log import logger


def _get_next_url(result) -> str | None:
    """Return ``next_url`` from a Pydantic model or plain dict result."""
    return safe_get(result, "next_url")


def _get_novels(result) -> list:
    """Return the ``novels`` list from a Pydantic model or plain dict result."""
    return safe_get(result, "novels") or []


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

    async def webview_novel(self, novel_id: int) -> dict | None:
        """Fetch a single novel with full text content."""
        return await self._call("webview_novel", novel_id)

    async def user_novels(
        self,
        author_id: int,
        fetch_all: bool = False,
    ) -> dict:
        """Fetch all novels by an author."""
        return await self._call(
            "user_novels", author_id, fetch_all=fetch_all,
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

    async def novel_follow(
        self,
        fetch_til=None,
    ) -> dict:
        """Fetch novels from followed users."""
        return await self._call(
            "novel_follow", fetch_til=fetch_til,
        )

    async def novel_ranking(
        self,
        mode: str = "day_r18",
        date=None,
        fetch_all: bool = False,
    ) -> dict:
        """Fetch novel rankings."""
        return await self._call(
            "novel_ranking", mode=mode, date=date, fetch_all=fetch_all,
        )

    async def search_novel(
        self,
        keyword: str,
        search_type: str = "keyword",
        order: str = "popular_desc",
        start_date=None,
        end_date=None,
        fetch_minlike: int | None = None,
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
        )

    async def novel_series(
        self,
        series_id: int,
        fetch_all: bool = False,
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

    # Pagination guard — never follow more than this many pages.
    _MAX_PAGES: int = 200

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
            except (PixivApiError, RequestException, ConnectionError) as e:
                # PixivApiError covers API-level errors translated by
                # account.execute(); RequestException covers requests' own
                # network errors; builtin ConnectionError is kept as a
                # network-error stand-in used by the existing test suite.
                logger.warning(
                    f"API error on {account}: {e}, "
                    f"retry {attempt + 1}/{self._MAX_RETRIES + 1}"
                )
                last_error = e
                if attempt < self._MAX_RETRIES:
                    await asyncio.sleep(self._backoff(attempt))

        raise last_error  # type: ignore[misc]

    async def _call(
        self,
        method: str,
        *args,
        fetch_all: bool = False,
        fetch_til=None,
        fetch_minlike: int | None = None,
        **kwargs,
    ):
        """Execute an API call with optional pagination."""
        async with self._semaphore:
            result = await self._execute_with_retry(method, *args, **kwargs)

        if result is None:
            return None

        if not (fetch_all or fetch_til or fetch_minlike):
            return result

        return await self._paginate(
            method, result, fetch_til, fetch_minlike
        )

    async def _paginate(
        self, method, result, fetch_til, fetch_minlike
    ):
        """Follow ``next_url`` across pages, collecting results.

        Each page goes back through ``pool.select()`` — normal tasks
        rotate accounts by LRU, while tasks running under
        ``account_rule(force_follow=True)`` (e.g. the daily ``novel_follow``
        update, which pins the designated「追更账号」) stay on that account.
        """
        page = 1

        novels_count = len(_get_novels(result))
        logger.info(f"Paginate {method}: page {page} — {novels_count} items")

        next_url = _get_next_url(result)
        while next_url:
            page += 1
            if page > self._MAX_PAGES:
                logger.warning(
                    f"Paginate {method}: exceeded {self._MAX_PAGES} pages, "
                    f"stopping",
                )
                break

            # pixivpy3's parse_qs flattens single-value params; replicate it
            # here so pagination works for dict- and model-shaped results.
            next_qs = {
                k: v[0] for k, v in parse_qs(urlparse(next_url).query).items()
            }

            async with self._semaphore:
                next_result = await self._execute_with_retry(
                    method, **next_qs
                )

            next_novels = _get_novels(next_result)
            new_next_url = _get_next_url(next_result)

            # Accumulate into the result, whatever its shape.
            safe_set(result, "novels", _get_novels(result) + next_novels)
            safe_set(result, "next_url", new_next_url)

            novels_count += len(next_novels)
            logger.info(
                f"Paginate {method}: page {page} — "
                f"{len(next_novels)} items (total: {novels_count})",
            )

            next_url = new_next_url

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

        return result

    @staticmethod
    def _should_stop(last_item, fetch_til, fetch_minlike) -> bool:
        """Determine if pagination should stop based on date or bookmark thresholds."""
        if fetch_til:
            create_dt = safe_get(last_item, "create_date")
            if create_dt is not None:
                try:
                    create_dt = date_parser.parse(create_dt)
                    # Pixiv create_date is naive while fetch_til comes from
                    # datetime.now().astimezone(); comparing naive vs aware raises
                    # TypeError, so strip the tzinfo whenever they disagree.
                    if create_dt.tzinfo is not None and fetch_til.tzinfo is None:
                        create_dt = create_dt.replace(tzinfo=None)
                    elif create_dt.tzinfo is None and fetch_til.tzinfo is not None:
                        fetch_til = fetch_til.replace(tzinfo=None)
                    if create_dt < fetch_til:
                        return True
                except Exception:
                    # Unparseable/ambiguous dates must never interrupt pagination.
                    return False
        if fetch_minlike:
            bookmarks = safe_get(last_item, "total_bookmarks")
            if bookmarks is not None and bookmarks < fetch_minlike:
                return True
        return False
