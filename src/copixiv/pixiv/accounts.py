"""Account pool — manages multiple Pixiv accounts with LRU selection."""

from __future__ import annotations

import asyncio
import threading
import time
from contextvars import ContextVar, Token

import httpx

from copixiv.log import logger

from .account import (
    AccountInvalidError,
    AccountStatus,
    AccountStrategy,
    PixivAccount,
    TokenInfo,
)
from .errors import PremiumRequiredError
from .http import normalize_proxy_url


class AccountPool:
    """A pool of Pixiv accounts with Least-Recently-Used (LRU) selection.

    Each call to ``select()`` picks the non-cooldown account with the
    smallest ``last_req_time`` — the one that has been idle the longest.
    This naturally distributes load evenly without needing a manual index.

    Strategy is carried via a ``ContextVar`` so nested calls can
    temporarily override the account selection.
    """

    def __init__(
        self,
        proxy_url: str | None = "",
        min_interval: float = 2.0,
        cooling_duration: float = 120.0,
        http_client: httpx.AsyncClient | None = None,
    ):
        self._accounts: list[PixivAccount] = []
        self._lock = threading.Lock()
        self._proxy_url = normalize_proxy_url(proxy_url) or ""
        self._min_interval = min_interval
        self._cooling_duration = cooling_duration
        self._http_client = http_client
        self._strategy: ContextVar[AccountStrategy | None] = ContextVar("strategy", default=None)

    def add_account(self, account: PixivAccount) -> None:
        with self._lock:
            self._accounts.append(account)

    @property
    def accounts(self) -> list[PixivAccount]:
        with self._lock:
            return list(self._accounts)

    def reload_from_tokens(self, tokens: list[dict]) -> None:
        """Rebuild the in-memory pool from persisted token rows.

        Reuses existing :class:`PixivAccount` instances (preserving active OAuth
        sessions, rate-limit cooldowns, and request locks) whenever the refresh
        token string matches; disabled rows (``valid=False``) are excluded.
        """
        with self._lock:
            existing = {a.token_info.token: a for a in self._accounts}
            rebuilt: list[PixivAccount] = []
            for entry in tokens:
                if not entry.get("valid", True):
                    continue
                tok = entry["token"]
                name = entry["name"]
                premium = bool(entry.get("premium", False))
                follow = bool(entry.get("is_follow", False))
                acc = existing.get(tok)
                if acc is not None:
                    acc.username = name
                    acc.token_info.username = name
                    acc.token_info.premium = premium
                    acc.token_info.valid = True
                    acc.token_info.follow = follow
                    if acc.status == AccountStatus.INVALID:
                        acc.status = AccountStatus.INACTIVE
                        acc._last_auth_time = 0.0
                        acc._access_token = None
                    rebuilt.append(acc)
                else:
                    rebuilt.append(
                        PixivAccount(
                            token_info=TokenInfo(
                                token=tok,
                                username=name,
                                premium=premium,
                                valid=True,
                                follow=follow,
                            ),
                            proxy_url=self._proxy_url,
                            min_interval=self._min_interval,
                            cooling_duration=self._cooling_duration,
                            http_client=self._http_client,
                        )
                    )
            self._accounts = rebuilt
            logger.info(f"Reloaded AccountPool: {len(rebuilt)} valid accounts")

    def select(self, strategy: AccountStrategy | None = None) -> PixivAccount:
        """Select the least-recently-used non-cooldown account.

        Accounts do not need to be pre-authenticated — ``execute()``
        triggers ``authenticate()`` on first use of an inactive account.
        """
        strat = strategy or self._strategy.get() or AccountStrategy()

        with self._lock:
            candidates = [a for a in self._accounts if a.valid]
            if not candidates:
                raise RuntimeError("No valid Pixiv accounts available")

            # Premium requirement — fatal when unmet.
            if strat.need_premium:
                premium = [a for a in candidates if a.token_info.premium]
                if not premium:
                    raise PremiumRequiredError(
                        "该任务需要 pixiv 会员账号（会员已到期或账号池里没有 premium 账号），已停止执行"
                    )
                candidates = premium

            # Force the designated「追更账号」(is_follow on the tokens table).
            if strat.force_follow:
                flagged = [a for a in candidates if a.token_info.follow]
                if flagged:
                    ready_flagged = [a for a in flagged if not a.in_cooldown]
                    if ready_flagged:
                        chosen = min(ready_flagged, key=lambda a: a.last_req_time)
                    else:
                        chosen = min(flagged, key=lambda a: a.cooldown_remaining)
                        wait = chosen.cooldown_remaining
                        if wait > 0:
                            logger.warning(
                                f"追更账号 {chosen} 处于冷却中，execute 将等待 {wait:.0f} 秒",
                            )
                    chosen.last_req_time = time.monotonic()
                    return chosen

            # Force a specific account (deprecated legacy string selector)
            if strat.force_account:
                forced = [
                    a
                    for a in candidates
                    if a.username == strat.force_account
                    or a.token_info.token == strat.force_account
                    or a.token_info.username == strat.force_account
                ]
                if forced:
                    ready_forced = [a for a in forced if not a.in_cooldown]
                    chosen = (
                        min(ready_forced, key=lambda a: a.last_req_time)
                        if ready_forced
                        else min(forced, key=lambda a: a.cooldown_remaining)
                    )
                    chosen.last_req_time = time.monotonic()
                    return chosen

            # LRU over all non-cooldown candidates.
            ready = [a for a in candidates if not a.in_cooldown]
            if not ready:
                best = min(candidates, key=lambda a: a.cooldown_remaining)
                wait = best.cooldown_remaining
                if wait > 0:
                    logger.warning(
                        f"返回冷却中的账号 {best}，execute 将等待 {wait:.0f} 秒",
                    )
                return best

            chosen = min(ready, key=lambda a: a.last_req_time)
            idle = time.monotonic() - chosen.last_req_time if chosen.last_req_time else -1
            chosen.last_req_time = time.monotonic()
            logger.debug(
                f"Account LRU: {chosen} (was idle {idle:.0f}s, {len(ready)}/{len(candidates)} ready)",
            )
            return chosen

    async def authenticate_all(self) -> None:
        """Authenticate all accounts in parallel."""
        accounts = self.accounts
        results = await asyncio.gather(
            *[a.authenticate() for a in accounts],
            return_exceptions=True,
        )
        for account, result in zip(accounts, results, strict=True):
            if isinstance(result, AccountInvalidError):
                logger.warning(f"Account {account} is invalid: {result}")
            elif isinstance(result, Exception):
                logger.error(f"Failed to authenticate {account}: {result}")

    async def aclose(self) -> None:
        """Close account-owned HTTP clients."""
        for account in self.accounts:
            await account.aclose()

    def set_strategy(self, strategy: AccountStrategy) -> Token[AccountStrategy | None]:
        """Temporarily override the account strategy. Returns a reset token."""
        return self._strategy.set(strategy)

    def reset_strategy(self, token: Token[AccountStrategy | None]) -> None:
        self._strategy.reset(token)
