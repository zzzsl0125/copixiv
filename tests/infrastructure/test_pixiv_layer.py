"""Tests for the pixiv infrastructure layer — no real network.

Covers the three pieces with no existing coverage:
- AccountPool: LRU selection, premium filter, forced account, cooldown
- PixivClient: retry / exponential backoff / account switching
- pixivpy3 monkey patches: fault-tolerant fallbacks
"""

from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from pixivpy3 import AppPixivAPI, PixivError

from copixiv.infrastructure.pixiv.account import (
    PixivAccount, TokenInfo, AccountStrategy, AccountStatus,
    AccountInvalidError, RateLimitError,
)
from copixiv.infrastructure.pixiv.accounts import AccountPool
from copixiv.infrastructure.pixiv.client import PixivClient
from copixiv.infrastructure.pixiv import patch as pixiv_patch


def _account(username: str, premium: bool = False) -> PixivAccount:
    """Real PixivAccount — construction never touches the network."""
    return PixivAccount(
        token_info=TokenInfo(token="t", username=username, premium=premium),
    )


class TestAccountPool:
    def test_lru_selects_least_recently_used(self):
        pool = AccountPool()
        a, b, c = _account("aaa"), _account("bbb"), _account("ccc")
        pool.add_account(a)
        pool.add_account(b)
        pool.add_account(c)
        a.last_req_time = 30.0
        b.last_req_time = 10.0
        c.last_req_time = 20.0

        assert pool.select() is b  # idle longest
        assert pool.select() is c  # b was just marked used
        assert pool.select() is a

    def test_premium_filter(self):
        pool = AccountPool()
        free = _account("free")
        premium = _account("prem", premium=True)
        pool.add_account(free)
        pool.add_account(premium)

        assert pool.select(AccountStrategy(need_premium=True)) is premium
        # Without the requirement, LRU picks the free one (idle longest)
        assert pool.select(AccountStrategy()) is free

    def test_force_account_by_username(self):
        pool = AccountPool()
        a, b = _account("alpha"), _account("beta")
        pool.add_account(a)
        pool.add_account(b)

        assert pool.select(AccountStrategy(force_account="beta")) is b

    def test_skips_invalid_accounts(self):
        pool = AccountPool()
        bad, good = _account("bad"), _account("good")
        bad.status = AccountStatus.INVALID
        pool.add_account(bad)
        pool.add_account(good)

        assert pool.select() is good

    def test_no_valid_accounts_raises(self):
        pool = AccountPool()
        bad = _account("bad")
        bad.status = AccountStatus.INVALID
        pool.add_account(bad)
        with pytest.raises(RuntimeError, match="No valid"):
            pool.select()

    def test_all_in_cooldown_picks_soonest_recovery(self):
        pool = AccountPool()
        a, b = _account("aaa"), _account("bbb")
        pool.add_account(a)
        pool.add_account(b)
        a.start_cooldown(120.0)   # recovers later
        b.start_cooldown(30.0)    # recovers sooner

        assert pool.select() is b

    def test_strategy_contextvar_resets(self):
        pool = AccountPool()
        a, b = _account("aaa"), _account("bbb")
        pool.add_account(a)
        pool.add_account(b)
        token = pool.set_strategy(AccountStrategy(force_account="bbb"))
        try:
            assert pool.select() is b
        finally:
            pool.reset_strategy(token)
        # back to plain LRU
        assert pool.select() in (a, b)


class _ScriptedPool:
    """Pool whose select() walks a fixed account sequence (repeats last)."""

    def __init__(self, accounts):
        self._accounts = list(accounts)
        self._i = 0

    def select(self):
        acc = self._accounts[min(self._i, len(self._accounts) - 1)]
        self._i += 1
        return acc


def _account_raising(*errors):
    """Fake account whose execute() raises *errors* in sequence, then
    returns 'ok' once exhausted."""
    state = {"calls": 0, "errors": list(errors), "method": None, "args": None}

    async def execute(method, *args, **kwargs):
        state["calls"] += 1
        state["method"] = method
        state["args"] = args
        if state["errors"]:
            raise state["errors"].pop(0)
        return "ok"

    return SimpleNamespace(execute=execute, state=state)


@pytest.fixture
def sleep_recorder(monkeypatch):
    """Replace asyncio.sleep with a recorder; returns the recorded list."""
    sleeps: list[float] = []

    async def _fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("asyncio.sleep", _fake_sleep)
    return sleeps


class TestClientRetry:
    """Retry/backoff/account-switch behaviour with scripted fake accounts."""

    async def test_retries_rate_limit_then_succeeds(self, sleep_recorder):
        acc = _account_raising(RateLimitError("rl"))
        client = PixivClient(_ScriptedPool([acc]))

        result = await client._execute_with_retry("webview_novel", 42)

        assert result == "ok"
        assert acc.state["calls"] == 2
        assert acc.state["method"] == "webview_novel"
        assert acc.state["args"] == (42,)
        assert sleep_recorder == [1.0]  # attempt-0 backoff before retry

    async def test_exponential_backoff_sequence(self, sleep_recorder):
        acc = _account_raising(*[RateLimitError("rl")] * 4)
        client = PixivClient(_ScriptedPool([acc]))

        with pytest.raises(RateLimitError):
            await client._execute_with_retry("novel_ranking")

        assert acc.state["calls"] == 4  # MAX_RETRIES + 1
        assert sleep_recorder == [1.0, 2.0, 4.0]  # doubled, capped at 10

    async def test_switches_account_on_invalid(self, sleep_recorder):
        bad = _account_raising(AccountInvalidError("bad"))
        good = _account_raising()
        client = PixivClient(_ScriptedPool([bad, good]))

        result = await client._execute_with_retry("user_detail", 1)

        assert result == "ok"
        assert bad.state["calls"] == 1
        assert good.state["calls"] == 1
        # invalid accounts are not retried with backoff — select() skips them
        assert sleep_recorder == []

    async def test_generic_error_is_retried(self, sleep_recorder):
        acc = _account_raising(ConnectionError("timeout"), ConnectionError("again"))
        client = PixivClient(_ScriptedPool([acc]))

        result = await client._execute_with_retry("user_novels", 1)

        assert result == "ok"
        assert sleep_recorder == [1.0, 2.0]

    async def test_exhaustion_raises_last_error(self, sleep_recorder):
        # One error per attempt (MAX_RETRIES + 1 = 4) so the retry loop
        # truly exhausts instead of succeeding on the second attempt.
        acc = _account_raising(*[ConnectionError("boom")] * 4)
        client = PixivClient(_ScriptedPool([acc]))

        with pytest.raises(ConnectionError, match="boom"):
            await client._execute_with_retry("user_novels", 1)
        assert acc.state["calls"] == 4
        assert sleep_recorder == [1.0, 2.0, 4.0]


class TestPixivPatches:
    """Monkey-patch tolerance — no network involved."""

    def test_apply_is_idempotent(self):
        pixiv_patch.apply()
        pixiv_patch.apply()  # second call is a no-op, must not raise
        assert pixiv_patch._patches_applied is True

    def test_permissive_model_construct_fallback_chain(self):
        from copixiv.infrastructure.pixiv.patch import _permissive_model_construct

        class _M(BaseModel):
            name: str
            count: int

        # 1. valid data → normal validation
        assert _permissive_model_construct(
            {"name": "x", "count": 1}, _M,
        ).name == "x"

        # 2. API error dict → passed through untouched
        err = {"error": {"message": "rate limit"}}
        assert _permissive_model_construct(err, _M) is err

        # 3. None in a required str field → sanitised to "" and validated
        m = _permissive_model_construct({"name": None, "count": 1}, _M)
        assert m.name == "" and m.count == 1

        # 4. totally invalid → model_construct fallback (never raises)
        m2 = _permissive_model_construct({"count": "nope"}, _M)
        assert m2 is not None

    def test_webview_patch_returns_none_on_content_error(self, monkeypatch):
        def _raise_content_error(self, novel_id):
            raise PixivError("failed to extract novel content from response")

        monkeypatch.setattr(AppPixivAPI, "webview_novel", _raise_content_error)
        monkeypatch.setattr(pixiv_patch, "_patches_applied", False)
        pixiv_patch.apply()

        obj = object.__new__(AppPixivAPI)
        assert AppPixivAPI.webview_novel(obj, 12345) is None

    def test_webview_patch_reraises_other_errors(self, monkeypatch):
        def _raise_other(self, novel_id):
            raise PixivError("some unrelated pixiv error")

        monkeypatch.setattr(AppPixivAPI, "webview_novel", _raise_other)
        monkeypatch.setattr(pixiv_patch, "_patches_applied", False)
        pixiv_patch.apply()

        obj = object.__new__(AppPixivAPI)
        with pytest.raises(PixivError, match="unrelated"):
            AppPixivAPI.webview_novel(obj, 12345)
