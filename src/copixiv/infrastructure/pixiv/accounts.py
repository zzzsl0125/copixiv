"""Account pool — manages multiple Pixiv accounts with selection strategy."""

import asyncio
import logging
import random
from contextvars import ContextVar

from .account import (
    PixivAccount,
    AccountStrategy,
    AccountInvalidError,
    RateLimitError,
)

logger = logging.getLogger("copixiv")


class AccountPool:
    """A pool of Pixiv accounts with strategy-based selection.

    Strategy is carried via a ``ContextVar`` so nested calls can
    temporarily override the account selection without threading issues.
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
        """Select the best available account according to *strategy*."""
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
                candidates = forced

        # Pick an available (non-cooldown, authenticated) account
        available = [a for a in candidates if a.available]
        if not available:
            # All in cooldown — pick the one with shortest remaining time
            best = min(candidates, key=lambda a: a.cooldown_remaining)
            wait = best.cooldown_remaining
            if wait > 0:
                logger.warning(
                    f"All accounts in cooldown, waiting {wait:.0f}s for {best}"
                )
            return best

        return random.choice(available)

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
