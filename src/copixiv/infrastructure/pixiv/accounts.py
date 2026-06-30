"""Account pool — manages multiple Pixiv accounts with LRU selection."""

import asyncio
import time
from contextvars import ContextVar

from .account import (
    PixivAccount,
    AccountStrategy,
    AccountInvalidError,
    RateLimitError,
)

from copixiv.app.logger import logger


class AccountPool:
    """A pool of Pixiv accounts with Least-Recently-Used (LRU) selection.

    Each call to ``select()`` picks the non-cooldown account with the
    smallest ``last_req_time`` — the one that has been idle the longest.
    This naturally distributes load evenly without needing a manual index.

    Strategy is carried via a ``ContextVar`` so nested calls can
    temporarily override the account selection.
    """

    def __init__(self):
        self._accounts: list[PixivAccount] = []
        self._strategy: ContextVar[AccountStrategy] = ContextVar(
            "strategy", default=AccountStrategy()
        )

    def add_account(self, account: PixivAccount) -> None:
        self._accounts.append(account)

    @property
    def accounts(self) -> list[PixivAccount]:
        return self._accounts

    def select(self, strategy: AccountStrategy | None = None) -> PixivAccount:
        """Select the least-recently-used non-cooldown account.

        Accounts do not need to be pre-authenticated — ``execute()``
        triggers ``authenticate()`` on first use of an inactive account.
        """
        strat = strategy or self._strategy.get()

        candidates = [a for a in self._accounts if a.valid]
        if not candidates:
            raise RuntimeError("No valid Pixiv accounts available")

        # Filter by premium requirement
        if strat.need_premium:
            premium = [a for a in candidates if a.token_info.premium]
            if premium:
                candidates = premium

        # Force a specific account
        if strat.force_account:
            forced = [
                a
                for a in candidates
                if a.username == strat.force_account
                or a.token_info.token == strat.force_account
                or a.token_info.username == strat.force_account
            ]
            if forced:
                return forced[0]

        # LRU over all non-cooldown candidates (not just "available" /
        # ACTIVE).  Unauthenticated accounts have last_req_time == 0.0,
        # so they naturally get picked before any used account.
        ready = [a for a in candidates if not a.in_cooldown]
        if not ready:
            # All in cooldown — pick the one that recovers soonest
            best = min(candidates, key=lambda a: a.cooldown_remaining)
            wait = best.cooldown_remaining
            if wait > 0:
                logger.warning(
                    f"All accounts in cooldown, waiting {wait:.0f}s for {best}",
                )
            return best

        # LRU: pick the account used least recently.
        # Update last_req_time immediately so concurrent selectors won't
        # pick the same account (V1's pattern).
        chosen = min(ready, key=lambda a: a.last_req_time)
        idle = time.time() - chosen.last_req_time if chosen.last_req_time else -1
        chosen.last_req_time = time.time()
        logger.debug(
            f"Account LRU: {chosen} (was idle {idle:.0f}s, "
            f"{len(ready)}/{len(candidates)} ready)",
        )
        return chosen

    async def authenticate_all(self) -> None:
        """Authenticate all accounts in parallel."""
        results = await asyncio.gather(
            *[a.authenticate() for a in self._accounts],
            return_exceptions=True,
        )
        for account, result in zip(self._accounts, results):
            if isinstance(result, AccountInvalidError):
                logger.warning(f"Account {account} is invalid: {result}")
            elif isinstance(result, Exception):
                logger.error(f"Failed to authenticate {account}: {result}")

    def set_strategy(self, strategy: AccountStrategy) -> ContextVar:
        """Temporarily override the account strategy. Returns a reset token."""
        return self._strategy.set(strategy)

    def reset_strategy(self, token) -> None:
        self._strategy.reset(token)
