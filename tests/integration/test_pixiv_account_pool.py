"""Layer 3 integration tests: AccountPool LRU selection, follow cooldown fallback, and premium guard."""

from __future__ import annotations

import pytest

from copixiv.pixiv.account import AccountStrategy, PixivAccount, TokenInfo
from copixiv.pixiv.accounts import AccountPool
from copixiv.pixiv.errors import PremiumRequiredError


def _make_account(
    name: str, *, premium: bool = False, follow: bool = False, valid: bool = True,
) -> PixivAccount:
    return PixivAccount(
        token_info=TokenInfo(
            token=f"tok_{name}",
            username=name,
            premium=premium,
            valid=valid,
            follow=follow,
        ),
        min_interval=0.0,
        cooling_duration=60.0,
    )


def test_account_pool_lru_rotation():
    pool = AccountPool()
    a1 = _make_account("a1")
    a2 = _make_account("a2")
    a3 = _make_account("a3")
    pool.add_account(a1)
    pool.add_account(a2)
    pool.add_account(a3)

    picked = [pool.select().username for _ in range(3)]
    assert picked == ["a1", "a2", "a3"]
    # Fourth pick returns to the least recently used (a1)
    assert pool.select().username == "a1"


def test_account_pool_force_follow_prefers_flagged_and_falls_back_on_cooldown():
    pool = AccountPool()
    normal = _make_account("normal", follow=False)
    follower = _make_account("follower", follow=True)
    pool.add_account(normal)
    pool.add_account(follower)

    chosen = pool.select(AccountStrategy(force_follow=True))
    assert chosen is follower
    assert chosen.last_req_time > 0.0

    # Put follow account in cooldown -> falls back to normal account instead of failing
    follower.start_cooldown(60.0)
    fallback = pool.select(AccountStrategy(force_follow=True))
    assert fallback is normal


def test_account_pool_need_premium_raises_when_absent_and_selects_when_present():
    pool = AccountPool()
    free = _make_account("free", premium=False)
    pool.add_account(free)

    with pytest.raises(PremiumRequiredError):
        pool.select(AccountStrategy(need_premium=True))

    prem = _make_account("vip", premium=True)
    pool.add_account(prem)
    assert pool.select(AccountStrategy(need_premium=True)) is prem
