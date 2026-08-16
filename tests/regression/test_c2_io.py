"""C2 复现：冷却机制失效 + Pixiv IO 层 / Telegram 的 Minor 问题。

覆盖：
- 冷却真正落到执行路径（execute 强制等待，start_cooldown(0) 清零）。
- 时钟统一 + select 全冷却返回恢复最快者。
- _execute_with_retry 只对可重试异常退避，编程错误直接抛出。
- _paginate 对裸 dict 回退、parse_qs 扁平化、最大页守卫。
- Telegram 异常日志 token 脱敏。

全部使用 asyncio.run 风格，不真实联网。
"""

import asyncio
import time

import httpx
import pytest

from copixiv.log import capture_logs
from copixiv.infrastructure.pixiv.account import PixivAccount, TokenInfo
from copixiv.infrastructure.pixiv.accounts import AccountPool
from copixiv.infrastructure.pixiv.client import PixivClient
from copixiv.infrastructure.notifier.telegram import TelegramNotifier


def _account(username: str) -> PixivAccount:
    return PixivAccount(
        token_info=TokenInfo(token="t", username=username),
    )


# ---------------------------------------------------------------------------
# C2: cooldown actually enforced on the execute path
# ---------------------------------------------------------------------------


def test_execute_waits_for_cooldown():
    """execute() 遇到冷却账号时必须等待冷却剩余时间再继续。"""

    async def scenario():
        a = _account("cooldown")
        a.api.auth = lambda **kwargs: None  # no-op authentication
        a.api.dummy = lambda: 1
        a._cooldown_until = time.monotonic() + 0.3

        start = time.monotonic()
        result = await a.execute("dummy")
        elapsed = time.monotonic() - start

        assert result == 1
        assert elapsed >= 0.25, f"冷却未生效，实际等待 {elapsed:.3f}s"

    asyncio.run(scenario())


def test_start_cooldown_zero_clears_cooldown():
    """duration=0 必须显式清零冷却，而不是回退到默认 cooling_duration。"""
    a = _account("zero")
    a.start_cooldown(0)
    assert not a.in_cooldown


def test_select_all_cooldown_returns_soonest_recovery():
    """全部冷却时 select() 返回恢复最早的账号（不等待）。"""
    pool = AccountPool()
    a = _account("aaa")
    b = _account("bbb")
    pool.add_account(a)
    pool.add_account(b)

    a._cooldown_until = time.monotonic() + 120.0
    b._cooldown_until = time.monotonic() + 30.0

    assert pool.select() is b


# ---------------------------------------------------------------------------
# Retry scope: programming errors must not be retried
# ---------------------------------------------------------------------------


def test_retry_does_not_retry_type_error():
    """TypeError 等编程错误应立即抛出，且只调用一次 execute。"""

    async def scenario():
        calls = {"n": 0}

        class FakeAccount:
            async def execute(self, method, *args, **kwargs):
                calls["n"] += 1
                raise TypeError("programming bug")

        class FakePool:
            def select(self):
                return FakeAccount()

        client = PixivClient(FakePool())
        with pytest.raises(TypeError, match="programming bug"):
            await client._execute_with_retry("dummy")

        assert calls["n"] == 1

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# _paginate robustness
# ---------------------------------------------------------------------------


def test_paginate_handles_dict_result_and_flattens_parse_qs():
    """裸 dict 结果不应触发 AttributeError，且 parse_qs 被扁平化传参。"""

    async def scenario():
        calls = {"kwargs": None, "n": 0}

        class FakeAccount:
            async def execute(self, method, *args, **kwargs):
                calls["n"] += 1
                calls["kwargs"] = kwargs
                return {"novels": [{"id": 1}], "next_url": None}

        class FakePool:
            def select(self):
                return FakeAccount()

        client = PixivClient(FakePool())
        result = {"novels": [], "next_url": "https://x/?p=2&offset=10"}
        out = await client._paginate("search_novel", result, None, None)

        assert isinstance(out, dict)
        assert out["novels"] == [{"id": 1}]
        assert out["next_url"] is None
        # parse_qs 返回 list[str]，须扁平化为标量再传给 execute
        assert calls["kwargs"] == {"p": "2", "offset": "10"}
        assert calls["n"] == 1

    asyncio.run(scenario())


def test_paginate_stops_at_max_pages(monkeypatch):
    """超过最大页数时中断，避免无界分页。"""

    calls = {"n": 0}

    class FakeAccount:
        async def execute(self, method, *args, **kwargs):
            calls["n"] += 1
            return {"novels": [], "next_url": "https://x/?p=2"}

    class FakePool:
        def select(self):
            return FakeAccount()

    monkeypatch.setattr(PixivClient, "_MAX_PAGES", 3)

    async def scenario():
        client = PixivClient(FakePool())
        result = {"novels": [], "next_url": "https://x/?p=2"}
        await client._paginate("search_novel", result, None, None)

    asyncio.run(scenario())
    # 初始页 + 2 次翻页 = 共 3 页；第 4 页触发守卫中断。
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Telegram token redaction
# ---------------------------------------------------------------------------


def test_telegram_error_log_redacts_token():
    """Telegram 异常日志不得泄漏 bot token。"""

    async def scenario():
        token = "123456:ABC"
        notifier = TelegramNotifier(token=token, chat_id="123")

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        request = httpx.Request("POST", url)
        response = httpx.Response(400, request=request)

        class FakeClient:
            async def post(self, *args, **kwargs):
                return response

        notifier._get_client = lambda: FakeClient()

        with capture_logs() as get_logs:
            await notifier._send_message("hello")
            logs = get_logs()

        assert "ABC" not in logs, f"token 泄漏进日志: {logs}"
        assert "***" in logs

    asyncio.run(scenario())
