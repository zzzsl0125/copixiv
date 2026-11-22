"""Account pool — manages multiple Pixiv accounts with LRU selection."""

from __future__ import annotations

import threading
import time
from contextvars import ContextVar, Token

import httpx

from copixiv.log import logger

from .account import (
    AccountStatus,
    AccountStrategy,
    PixivAccount,
    TokenInfo,
)
from .errors import PremiumRequiredError


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
        http_client: httpx.AsyncClient,
        min_interval: float = 2.0,
        cooling_duration: float = 120.0,
    ):
        self._accounts: list[PixivAccount] = []
        self._lock = threading.Lock()
        self._http_client = http_client
        self._min_interval = min_interval
        self._cooling_duration = cooling_duration
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
                            http_client=self._http_client,
                            min_interval=self._min_interval,
                            cooling_duration=self._cooling_duration,
                        )
                    )
            self._accounts = rebuilt
            logger.info(f"Reloaded AccountPool: {len(rebuilt)} valid accounts")

    def _matching_candidates(self, strat: AccountStrategy) -> list[PixivAccount]:
        """Return valid accounts matching *strat* (caller must hold ``self._lock``)."""
        candidates = [a for a in self._accounts if a.valid]
        if not candidates:
            raise RuntimeError("No valid Pixiv accounts available")

        if strat.need_premium:
            premium = [a for a in candidates if a.token_info.premium]
            if not premium:
                raise PremiumRequiredError(
                    "该任务需要 pixiv 会员账号（会员已到期或账号池里没有 premium 账号），已停止执行"
                )
            candidates = premium

        if strat.force_follow:
            flagged = [a for a in candidates if a.token_info.follow]
            if flagged:
                return flagged

        return candidates

    def has_ready_account(self) -> bool:
        """Return True if at least one account matching the active strategy is not in cooldown."""
        strat = self._strategy.get() or AccountStrategy()
        with self._lock:
            try:
                candidates = self._matching_candidates(strat)
            except (RuntimeError, PremiumRequiredError):
                return False
            return any(not a.in_cooldown for a in candidates)

    def select(self, strategy: AccountStrategy | None = None) -> PixivAccount:
        """Select the least-recently-used non-cooldown account.

        Accounts do not need to be pre-authenticated — ``request()``
        triggers ``authenticate()`` on first use of an inactive account.
        """
        strat = strategy or self._strategy.get() or AccountStrategy()

        with self._lock:
            candidates = self._matching_candidates(strat)

            ready = [a for a in candidates if not a.in_cooldown]
            if not ready:
                best = min(candidates, key=lambda a: (a.cooldown_remaining, a.last_req_time))
                wait = best.cooldown_remaining
                if wait > 0:
                    prefix = (
                        "追更账号 " if strat.force_follow and best.token_info.follow else "返回冷却中的账号 "
                    )
                    logger.warning(
                        f"{prefix}{best} 处于冷却中，request 将等待 {wait:.0f} 秒",
                    )
                best.last_req_time = time.monotonic()
                return best

            chosen = min(ready, key=lambda a: a.last_req_time)
            idle = time.monotonic() - chosen.last_req_time if chosen.last_req_time else -1
            chosen.last_req_time = time.monotonic()
            logger.debug(
                f"Account LRU: {chosen} (was idle {idle:.0f}s, {len(ready)}/{len(candidates)} ready)",
            )
            return chosen

    def set_strategy(self, strategy: AccountStrategy) -> Token[AccountStrategy | None]:
        """Temporarily override the account strategy. Returns a reset token."""
        return self._strategy.set(strategy)

    def reset_strategy(self, token: Token[AccountStrategy | None]) -> None:
        self._strategy.reset(token)
