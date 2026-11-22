"""Layer 1 unit tests: native httpx PixivClient, OAuth, retry/tenacity, and pagination."""

from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest
import respx

from copixiv.core.draft import build_from_webview
from copixiv.core.exceptions import NovelNotFoundError
from copixiv.pixiv.account import (
    AccountStatus,
    AccountStrategy,
    PixivAccount,
    TokenInfo,
)
from copixiv.pixiv.accounts import AccountPool
from copixiv.pixiv.client import PixivClient, _is_permanent
from copixiv.pixiv.errors import (
    AccountInvalidError,
    PixivApiError,
    RateLimitError,
)


def _make_pool(*names: str, http_client: httpx.AsyncClient | None = None) -> AccountPool:
    pool = AccountPool(min_interval=0.0, cooling_duration=60.0, http_client=http_client)
    for name in names:
        pool.add_account(
            PixivAccount(
                token_info=TokenInfo(token=f"tok_{name}", username=name, valid=True),
                min_interval=0.0,
                cooling_duration=60.0,
                http_client=http_client,
            )
        )
    return pool


def test_is_permanent_classification():
    assert _is_permanent(PixivApiError("not found", status_code=404)) is True
    assert _is_permanent(PixivApiError("forbidden", status_code=403)) is True
    assert _is_permanent(PixivApiError("unauthorized", status_code=401)) is False
    assert _is_permanent(PixivApiError("timeout", status_code=408)) is False
    assert _is_permanent(PixivApiError("rate limit", status_code=429)) is False
    assert _is_permanent(PixivApiError("server err", status_code=502)) is False
    assert _is_permanent(PixivApiError("unknown", status_code=None)) is False


@pytest.mark.asyncio
async def test_native_oauth_webview_and_api_endpoints_with_respx():
    async with httpx.AsyncClient() as http_client:
        pool = _make_pool("acc1", http_client=http_client)
        client = PixivClient(pool, max_concurrency=2)

        webview_json = {
            "id": "1001",
            "title": "测试小说",
            "userId": "88",
            "coverUrl": "https://img/cover.jpg",
            "tags": ["原创", "中文"],
            "caption": "简介",
            "cdate": "2026-06-01T12:00:00+09:00",
            "rating": {"like": 5, "bookmark": 120, "view": 3400},
            "text": "正文 [uploadedimage:11]",
            "seriesId": "500",
            "seriesTitle": "测试系列",
            "seriesNavigation": {"prevNovel": {"contentOrder": 2}},
            "images": [],
            "illusts": {},
        }
        webview_html = f"<html><script>novel: {json.dumps(webview_json)}, isOwnWork: false</script></html>"

        with respx.mock(assert_all_called=True) as mock:
            auth_route = mock.post("https://oauth.secure.pixiv.net/auth/token").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "response": {
                            "access_token": "acc_token_1",
                            "refresh_token": "tok_acc1",
                            "user": {"id": "88"},
                        }
                    },
                )
            )
            wv_route = mock.get("https://app-api.pixiv.net/webview/v2/novel").mock(
                return_value=httpx.Response(200, text=webview_html)
            )
            detail_route = mock.get("https://app-api.pixiv.net/v1/user/detail").mock(
                return_value=httpx.Response(
                    200,
                    json={"user": {"id": 88, "name": "作者甲", "account": "auth88"}},
                )
            )
            novels_route = mock.get("https://app-api.pixiv.net/v1/user/novels").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "novels": [
                            {
                                "id": 1001,
                                "title": "测试小说",
                                "caption": None,
                                "tags": [{"name": "中文"}],
                                "user": {"id": 88, "name": "作者甲"},
                                "series": {},
                                "total_bookmarks": 120,
                                "total_view": 3400,
                                "text_length": 20,
                                "create_date": "2026-06-01T12:00:00+09:00",
                            }
                        ],
                        "next_url": None,
                    },
                )
            )

            wv = await client.webview_novel(1001)
            draft = build_from_webview(wv)
            assert draft.id == 1001
            assert draft.author_id == 88
            assert draft.series_id == 500
            assert draft.series_index == 3
            assert draft.images is None

            detail = await client.user_detail(88)
            assert detail.user.name == "作者甲"

            listed = await client.user_novels(88)
            assert len(listed.novels) == 1
            assert listed.novels[0].caption == ""
            assert listed.novels[0].series is None

            assert auth_route.call_count == 1
            auth_req = auth_route.calls[0].request
            assert "x-client-time" in auth_req.headers
            assert "x-client-hash" in auth_req.headers
            assert wv_route.calls[0].request.headers["authorization"] == "Bearer acc_token_1"
            assert detail_route.call_count == 1
            assert novels_route.call_count == 1


@pytest.mark.asyncio
async def test_webview_404_and_missing_marker_fail_immediately_without_retry():
    async with httpx.AsyncClient() as http_client:
        pool = _make_pool("acc1", http_client=http_client)
        client = PixivClient(pool, max_concurrency=1)

        with respx.mock:
            mock_auth = respx.post("https://oauth.secure.pixiv.net/auth/token").mock(
                return_value=httpx.Response(
                    200,
                    json={"response": {"access_token": "tok_ok", "user": {"id": "1"}}},
                )
            )
            route_404 = respx.get("https://app-api.pixiv.net/webview/v2/novel").mock(
                return_value=httpx.Response(404, text="Not Found")
            )

            with pytest.raises(NovelNotFoundError):
                await client.webview_novel(999)
            assert route_404.call_count == 1
            assert mock_auth.call_count == 1

        with respx.mock:
            route_bad_html = respx.get("https://app-api.pixiv.net/webview/v2/novel").mock(
                return_value=httpx.Response(200, text="<html>deleted</html>")
            )
            with pytest.raises(NovelNotFoundError):
                await client.webview_novel(1000)
            assert route_bad_html.call_count == 1


@pytest.mark.asyncio
async def test_client_rotates_on_429_respects_retry_after_and_retries_401_and_502(
    monkeypatch: pytest.MonkeyPatch,
):
    async def _no_sleep(_sec: float) -> None:
        return None

    monkeypatch.setattr("copixiv.pixiv.client.asyncio.sleep", _no_sleep)
    async with httpx.AsyncClient() as http_client:
        pool = _make_pool("acc1", "acc2", http_client=http_client)
        client = PixivClient(pool, max_concurrency=1)

        with respx.mock:
            respx.post("https://oauth.secure.pixiv.net/auth/token").mock(
                side_effect=[
                    httpx.Response(200, json={"response": {"access_token": "t1"}}),
                    httpx.Response(200, json={"response": {"access_token": "t2"}}),
                    httpx.Response(200, json={"response": {"access_token": "t2_reauth"}}),
                ]
            )
            detail_route = respx.get("https://app-api.pixiv.net/v1/user/detail").mock(
                side_effect=[
                    # acc1 hits 429 with Retry-After -> cools 45s & rotates to acc2
                    httpx.Response(429, headers={"Retry-After": "45"}),
                    # acc2 hits 401 -> re-auths on next retry
                    httpx.Response(401, json={"error": {"message": "Invalid token"}}),
                    # acc2 hits transient 502 -> retries
                    httpx.Response(502, text="Bad Gateway"),
                    # acc2 succeeds
                    httpx.Response(200, json={"user": {"id": 7, "name": "OkAuthor"}}),
                ]
            )

            res = await client.user_detail(7)
            assert res.user.name == "OkAuthor"
            assert detail_route.call_count == 4
            assert 40.0 <= pool.accounts[0].cooldown_remaining <= 45.0

        # Permanent 403 does not retry
        with respx.mock:
            forbidden = respx.get("https://app-api.pixiv.net/v1/user/detail").mock(
                return_value=httpx.Response(403, text="Forbidden")
            )
            with pytest.raises(PixivApiError) as exc_info:
                await client.user_detail(8)
            assert exc_info.value.status_code == 403
            assert forbidden.call_count == 1


@pytest.mark.asyncio
async def test_auth_400_marks_account_invalid_and_reload_revives(
    monkeypatch: pytest.MonkeyPatch,
):
    async def _no_sleep(_sec: float) -> None:
        return None

    monkeypatch.setattr("copixiv.pixiv.client.asyncio.sleep", _no_sleep)
    async with httpx.AsyncClient() as http_client:
        pool = _make_pool("bad", "good", http_client=http_client)
        client = PixivClient(pool, max_concurrency=1)

        with respx.mock:
            respx.post("https://oauth.secure.pixiv.net/auth/token").mock(
                side_effect=[
                    httpx.Response(400, json={"error": "invalid_grant"}),
                    httpx.Response(200, json={"response": {"access_token": "good_tok"}}),
                ]
            )
            respx.get("https://app-api.pixiv.net/v1/user/detail").mock(
                return_value=httpx.Response(200, json={"user": {"id": 1, "name": "Good"}})
            )
            res = await client.user_detail(1)
            assert res.user.name == "Good"
            assert pool.accounts[0].status == AccountStatus.INVALID

        # reload_from_tokens revives INVALID account when valid=True
        pool.reload_from_tokens([{"name": "bad", "token": "tok_bad", "valid": True}])
        assert pool.accounts[0].status == AccountStatus.INACTIVE

        # Auth error in JSON body re-authenticates once inside execute()
        with respx.mock:
            auth_route = respx.post("https://oauth.secure.pixiv.net/auth/token").mock(
                side_effect=[
                    httpx.Response(200, json={"response": {"access_token": "stale"}}),
                    httpx.Response(200, json={"response": {"access_token": "fresh"}}),
                ]
            )
            respx.get("https://app-api.pixiv.net/v1/user/detail").mock(
                side_effect=[
                    httpx.Response(
                        200,
                        json={"error": {"message": "Error occurred at the OAuth process"}},
                    ),
                    httpx.Response(200, json={"user": {"id": 2, "name": "Fresh"}}),
                ]
            )
            res2 = await client.user_detail(2)
            assert res2.user.name == "Fresh"
            assert auth_route.call_count == 2


@pytest.mark.asyncio
async def test_client_paginate_joins_pages_stops_on_thresholds_and_rejects_bad_next_origin():
    async with httpx.AsyncClient() as http_client:
        pool = _make_pool("acc1", http_client=http_client)
        client = PixivClient(pool, max_concurrency=1)

        with respx.mock:
            respx.post("https://oauth.secure.pixiv.net/auth/token").mock(
                return_value=httpx.Response(200, json={"response": {"access_token": "tok"}})
            )
            respx.get("https://app-api.pixiv.net/v1/search/novel", params__contains={"word": "tag"}).mock(
                side_effect=[
                    httpx.Response(
                        200,
                        json={
                            "novels": [
                                {"id": 1, "total_bookmarks": 500, "create_date": "2026-06-05T00:00:00"}
                            ],
                            "next_url": "https://app-api.pixiv.net/v1/search/novel?word=tag&offset=1",
                        },
                    ),
                    httpx.Response(
                        200,
                        json={
                            "novels": [
                                {"id": 2, "total_bookmarks": 50, "create_date": "2026-06-03T00:00:00"}
                            ],
                            "next_url": "https://app-api.pixiv.net/v1/search/novel?word=tag&offset=2",
                        },
                    ),
                ]
            )
            # Offset=1 handler for next_url
            respx.get(
                "https://app-api.pixiv.net/v1/search/novel", params={"word": "tag", "offset": "1"}
            ).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "novels": [{"id": 2, "total_bookmarks": 50, "create_date": "2026-06-03T00:00:00"}],
                        "next_url": "https://app-api.pixiv.net/v1/search/novel?word=tag&offset=2",
                    },
                )
            )
            respx.get("https://app-api.pixiv.net/v1/novel/follow").mock(
                side_effect=[
                    httpx.Response(
                        200,
                        json={
                            "novels": [
                                {"id": 1, "total_bookmarks": 500, "create_date": "2026-06-05T00:00:00"}
                            ],
                            "next_url": "https://app-api.pixiv.net/v1/novel/follow?offset=1",
                        },
                    ),
                    httpx.Response(
                        200,
                        json={
                            "novels": [
                                {"id": 2, "total_bookmarks": 50, "create_date": "2026-06-03T00:00:00"}
                            ],
                            "next_url": "https://app-api.pixiv.net/v1/novel/follow?offset=2",
                        },
                    ),
                ]
            )

            res = await client.search_novel("tag", fetch_minlike=100)
            assert [n.id for n in res.novels] == [1, 2]

            async with client.account_rule(force_follow=True):
                assert AccountStrategy(force_follow=True) == pool._strategy.get()

            res2 = await client.novel_follow(fetch_til=datetime(2026, 6, 4))
            assert [n.id for n in res2.novels] == [1, 2]

            # Off-origin next_url is rejected before issuing any HTTP call
            respx.get("https://app-api.pixiv.net/v1/user/novels").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "novels": [{"id": 10}],
                        "next_url": "https://evil.example/steal-token",
                    },
                )
            )
            evil_route = respx.get("https://evil.example/steal-token").mock(return_value=httpx.Response(200))
            with pytest.raises(PixivApiError, match="unexpected next_url origin"):
                await client.user_novels(10, fetch_all=True)
            assert evil_route.call_count == 0


@pytest.mark.asyncio
async def test_client_rate_limit_and_account_invalid_error_types():
    pool = _make_pool("acc1")
    client = PixivClient(pool, max_concurrency=1)

    async def _fail_rate_limit(self: PixivAccount, method: str, url: str, **kwargs):
        del self, method, url, kwargs
        raise RateLimitError("limited")

    orig = PixivAccount.request
    PixivAccount.request = _fail_rate_limit  # type: ignore[method-assign]
    try:
        pool.accounts[0].start_cooldown(0.0)
        client._MAX_RETRIES = 0
        with pytest.raises(RateLimitError):
            await client.user_detail(1)
    finally:
        PixivAccount.request = orig  # type: ignore[method-assign]

    async def _fail_invalid(self: PixivAccount, method: str, url: str, **kwargs):
        del self, method, url, kwargs
        raise AccountInvalidError("invalid")

    PixivAccount.request = _fail_invalid  # type: ignore[method-assign]
    try:
        with pytest.raises(AccountInvalidError):
            await client.user_detail(1)
    finally:
        PixivAccount.request = orig  # type: ignore[method-assign]
