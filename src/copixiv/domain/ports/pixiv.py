"""Pixiv client ports — abstract interfaces for the Pixiv API."""

from typing import Protocol
from collections.abc import AsyncIterator


class PixivNovelPort(Protocol):
    """Port for fetching novel data from Pixiv."""

    async def webview_novel(self, novel_id: int) -> dict: ...
    async def user_novels(
        self, author_id: int, fetch_all: bool = False
    ) -> dict: ...
    async def user_detail(self, user_id: int) -> dict: ...
    async def novel_follow(self, fetch_til=None) -> dict: ...
    async def novel_ranking(
        self, mode: str = "day_r18", date=None, fetch_all: bool = False
    ) -> dict: ...
    async def search_novel(
        self,
        keyword: str,
        search_type: str = "keyword",
        order: str = "popular_desc",
        start_date=None,
        end_date=None,
        fetch_minlike: int | None = None,
    ) -> dict: ...
    async def novel_series(
        self, series_id: int, fetch_all: bool = False
    ) -> dict: ...


class PixivAccountPort(Protocol):
    """Port for managing Pixiv account operations."""

    async def user_follow_add(self, user_id: int) -> dict: ...
    async def user_follow_delete(self, user_id: int) -> dict: ...

    def account_rule(
        self,
        need_premium: bool = False,
        force_account: str | None = None,
        force_follow: bool = False,
    ) -> AsyncIterator[None]: ...
