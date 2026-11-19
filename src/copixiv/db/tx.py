"""Transaction runners — the explicit bridge into the synchronous PostgreSQL layer."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from typing import Any

from sqlalchemy.exc import DBAPIError
from tenacity import RetryCallState, Retrying, retry_if_exception, stop_after_attempt

from copixiv.log import logger

# PostgreSQL transient concurrency errors:
#   55P03 — lock_not_available (lock_timeout)
#   40P01 — deadlock_detected
#   40001 — serialization_failure
_RETRYABLE_SQLSTATES: frozenset[str] = frozenset({"55P03", "40P01", "40001"})


def _is_retryable_db_error(exc: BaseException) -> bool:
    """True for transient PostgreSQL lock/deadlock/serialization conflicts."""
    if not isinstance(exc, DBAPIError):
        return False
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return code in _RETRYABLE_SQLSTATES


def _run_write_sync(
    uow,
    fn: Callable[[Any], Any],
    *,
    max_attempts: int,
    base_delay: float,
    max_delay: float,
) -> Any:
    """Synchronous body of :func:`run_write_transaction` (runs in a worker thread)."""
    attempts = max(max_attempts, 1)

    def _wait(retry_state: RetryCallState) -> float:
        attempt = max(retry_state.attempt_number - 1, 0)
        delay = min(base_delay * (2**attempt), max_delay)
        return delay * random.uniform(0.5, 1.5)

    def _before_sleep(retry_state: RetryCallState) -> None:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        delay = retry_state.next_action.sleep if retry_state.next_action else 0.0
        logger.warning(
            "写事务撞锁（第 {} / {} 次尝试），{:.3f}s 后重试：{}",
            retry_state.attempt_number,
            attempts,
            delay,
            exc,
        )

    for attempt in Retrying(
        stop=stop_after_attempt(attempts),
        retry=retry_if_exception(_is_retryable_db_error),
        wait=_wait,
        before_sleep=_before_sleep,
        sleep=time.sleep,
        reraise=True,
    ):
        with attempt, uow.begin():
            return fn(uow)

    raise AssertionError("unreachable: retry loop must return or raise")


def run_write_transaction_sync(
    uow,
    fn: Callable[[Any], Any],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.05,
    max_delay: float = 0.5,
) -> Any:
    """Synchronous variant of :func:`run_write_transaction` for sync callers."""
    return _run_write_sync(
        uow,
        fn,
        max_attempts=max_attempts,
        base_delay=base_delay,
        max_delay=max_delay,
    )


async def run_write_transaction(
    uow,
    fn: Callable[[Any], Any],
    *,
    max_attempts: int = 3,
    base_delay: float = 0.05,
    max_delay: float = 0.5,
) -> Any:
    """Run synchronous ``fn(uow)`` in one write transaction with tenacity retries."""
    return await asyncio.to_thread(
        _run_write_sync,
        uow,
        fn,
        max_attempts=max_attempts,
        base_delay=base_delay,
        max_delay=max_delay,
    )


def _run_read_sync(uow, fn: Callable[[Any], Any]) -> Any:
    with uow.begin(read_only=True):
        return fn(uow)


async def read_transaction(uow, fn: Callable[[Any], Any]) -> Any:
    """Run synchronous ``fn(uow)`` in one read-only transaction, off the loop."""
    return await asyncio.to_thread(_run_read_sync, uow, fn)


__all__ = [
    "_is_retryable_db_error",
    "read_transaction",
    "run_write_transaction",
    "run_write_transaction_sync",
]
