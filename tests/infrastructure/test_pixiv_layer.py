"""Tests for the pixiv infrastructure layer — no real network.

Covers the three pieces with no existing coverage:
- AccountPool: LRU selection, premium filter, forced account, cooldown
- PixivClient: retry / exponential backoff / account switching
- pixivpy3 monkey patches: fault-tolerant fallbacks + status-code errors
"""

from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from pixivpy3 import AppPixivAPI, PixivError
from pixivpy3.api import BasePixivAPI
from requests.structures import CaseInsensitiveDict

from copixiv.domain.exceptions import NovelNotFoundError
from copixiv.infrastructure.pixiv.account import (
    PixivAccount, TokenInfo, AccountStrategy, AccountStatus,
    AccountInvalidError, RateLimitError,
)
from copixiv.infrastructure.pixiv.accounts import AccountPool
from copixiv.infrastructure.pixiv.client import PixivClient
from copixiv.infrastructure.pixiv.errors import PixivApiError, PixivHttpError
from copixiv.infrastructure.pixiv import patch as pixiv_patch

# The true, unpatched requests_call — captured before any apply() runs.
_REAL_REQUESTS_CALL = BasePixivAPI.requests_call


def _account(username: str, premium: bool = False, follow: bool = False) -> PixivAccount:
    """Real PixivAccount — construction never touches the network."""
    return PixivAccount(
        token_info=TokenInfo(token="t", username=username, premium=premium, follow=follow),
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

    def test_force_follow_prefers_flagged_account(self):
        pool = AccountPool()
        a, b = _account("alpha"), _account("beta", follow=True)
        pool.add_account(a)
        pool.add_account(b)

        assert pool.select(AccountStrategy(force_follow=True)) is b

    def test_force_follow_skips_invalid_flagged_account(self):
        """A flagged account that is invalid must not be selected by force_follow."""
        pool = AccountPool()
        flagged = _account("flagged", follow=True)
        flagged.status = AccountStatus.INVALID
        good = _account("good")
        pool.add_account(flagged)
        pool.add_account(good)

        assert pool.select(AccountStrategy(force_follow=True)) is good

    def test_force_follow_no_flag_falls_back_to_lru(self):
        """No account flagged → force_follow degrades to normal LRU selection."""
        pool = AccountPool()
        a, b = _account("aaa"), _account("bbb")
        pool.add_account(a)
        pool.add_account(b)

        # LRU: idle-longest wins (both idle → first added, then a used).
        assert pool.select(AccountStrategy(force_follow=True)) is a

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

    def test_apply_is_idempotent(self, monkeypatch):
        # Reset through monkeypatch so the module-global flag is restored
        # after the test — later tests rely on the default False state.
        monkeypatch.setattr(pixiv_patch, "_patches_applied", False)
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

    def test_webview_patch_raises_not_found_on_content_error(self, monkeypatch):
        """Deterministic content failures are NovelNotFoundError, not None."""
        def _raise_content_error(self, novel_id):
            raise PixivError("failed to extract novel content from response")

        monkeypatch.setattr(AppPixivAPI, "webview_novel", _raise_content_error)
        monkeypatch.setattr(pixiv_patch, "_patches_applied", False)
        pixiv_patch.apply()

        obj = object.__new__(AppPixivAPI)
        with pytest.raises(NovelNotFoundError):
            AppPixivAPI.webview_novel(obj, 12345)

    def test_webview_patch_404_raises_not_found(self, monkeypatch):
        """HTTP 404 on webview is a deterministic NovelNotFoundError."""

        def _raise_404(self, novel_id):
            raise PixivHttpError("HTTP 404 for GET", status_code=404)

        monkeypatch.setattr(AppPixivAPI, "webview_novel", _raise_404)
        monkeypatch.setattr(pixiv_patch, "_patches_applied", False)
        pixiv_patch.apply()

        obj = object.__new__(AppPixivAPI)
        with pytest.raises(NovelNotFoundError):
            AppPixivAPI.webview_novel(obj, 12345)

    def test_webview_patch_reraises_http_429(self, monkeypatch):
        """429 must NOT be converted — it stays retryable."""

        def _raise_429(self, novel_id):
            raise PixivHttpError("HTTP 429 for GET", status_code=429)

        monkeypatch.setattr(AppPixivAPI, "webview_novel", _raise_429)
        monkeypatch.setattr(pixiv_patch, "_patches_applied", False)
        pixiv_patch.apply()

        obj = object.__new__(AppPixivAPI)
        with pytest.raises(PixivHttpError, match="429"):
            AppPixivAPI.webview_novel(obj, 12345)

    def test_requests_call_patch_raises_http_error_with_status(self, monkeypatch):
        """requests_call raises PixivHttpError carrying the status code."""
        monkeypatch.setattr(BasePixivAPI, "requests_call", _REAL_REQUESTS_CALL)
        monkeypatch.setattr(pixiv_patch, "_patches_applied", False)
        pixiv_patch.apply()

        obj = object.__new__(AppPixivAPI)
        obj.additional_headers = CaseInsensitiveDict()
        obj.requests_kwargs = {}
        obj.requests = SimpleNamespace(
            get=lambda *a, **k: SimpleNamespace(
                status_code=429, headers={}, text='{"error": {}}',
            ),
        )

        with pytest.raises(PixivHttpError) as excinfo:
            AppPixivAPI.requests_call(obj, "GET", "https://x/")
        assert excinfo.value.status_code == 429

    def test_requests_call_patch_passes_through_200(self, monkeypatch):
        monkeypatch.setattr(BasePixivAPI, "requests_call", _REAL_REQUESTS_CALL)
        monkeypatch.setattr(pixiv_patch, "_patches_applied", False)
        pixiv_patch.apply()

        resp = SimpleNamespace(status_code=200, headers={}, text="ok")
        obj = object.__new__(AppPixivAPI)
        obj.additional_headers = CaseInsensitiveDict()
        obj.requests_kwargs = {}
        obj.requests = SimpleNamespace(get=lambda *a, **k: resp)

        assert AppPixivAPI.requests_call(obj, "GET", "https://x/") is resp

    def test_webview_patch_reraises_other_errors(self, monkeypatch):
        def _raise_other(self, novel_id):
            raise PixivError("some unrelated pixiv error")

        monkeypatch.setattr(AppPixivAPI, "webview_novel", _raise_other)
        monkeypatch.setattr(pixiv_patch, "_patches_applied", False)
        pixiv_patch.apply()

        obj = object.__new__(AppPixivAPI)
        with pytest.raises(PixivError, match="unrelated"):
            AppPixivAPI.webview_novel(obj, 12345)


class TestAccountExecute:
    """PixivAccount.execute() error classification — no network."""

    def _ready_account(self, username: str = "acc") -> PixivAccount:
        acc = _account(username)
        acc.status = AccountStatus.ACTIVE
        acc._last_auth_time = 1e18  # fresh auth → authenticate() is a no-op
        acc.min_interval = 0.01
        return acc

    async def test_error_body_rate_limit_raises_and_starts_cooldown(self):
        acc = self._ready_account()
        acc.api = SimpleNamespace(
            auth=lambda **kw: None,
            novel_ranking=lambda **kw: {"error": {"message": "Rate Limit"}},
        )

        with pytest.raises(RateLimitError):
            await acc.execute("novel_ranking")
        assert acc.in_cooldown

    async def test_http_429_raises_and_starts_cooldown(self):
        acc = self._ready_account()

        def _boom(**kw):
            raise PixivHttpError("HTTP 429 for GET x", status_code=429)

        acc.api = SimpleNamespace(auth=lambda **kw: None, novel_ranking=_boom)

        with pytest.raises(RateLimitError):
            await acc.execute("novel_ranking")
        assert acc.in_cooldown

    async def test_http_401_forces_reauth_and_stays_retryable(self):
        acc = self._ready_account()

        def _boom(**kw):
            raise PixivHttpError("HTTP 401 for GET x", status_code=401)

        acc.api = SimpleNamespace(auth=lambda **kw: None, novel_ranking=_boom)

        with pytest.raises(PixivApiError):
            await acc.execute("novel_ranking")
        # Not invalidated; next attempt will re-auth with a fresh token.
        assert acc.status == AccountStatus.INACTIVE
        assert acc._last_auth_time == 0.0
        assert not acc.in_cooldown

    async def test_generic_error_body_raises_api_error(self):
        acc = self._ready_account()
        acc.api = SimpleNamespace(
            auth=lambda **kw: None,
            novel_ranking=lambda **kw: {
                "error": {"message": "something else"},
            },
        )

        with pytest.raises(PixivApiError):
            await acc.execute("novel_ranking")
        assert not acc.in_cooldown

    async def test_auth_body_error_retries_once_then_returns(self):
        """OAuth-flavoured error bodies trigger re-auth + one retry."""
        acc = self._ready_account()
        calls = {"n": 0}

        def _novel_ranking(**kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"error": {"message": "invalid_grant"}}
            return {"novels": []}

        acc.api = SimpleNamespace(auth=lambda **kw: None, novel_ranking=_novel_ranking)

        result = await acc.execute("novel_ranking")
        assert calls["n"] == 2
        assert result == {"novels": []}
