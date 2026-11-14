"""Layer 2 DB tests: SqlUnitOfWork guards, read/write transactions, and 55P03 lock-timeout retry."""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from copixiv.db import models
from copixiv.db.uow import SqlUnitOfWork
from copixiv.db.write_lock import read_transaction, run_write_transaction


def test_uow_session_guards_and_rollback_on_error(uow: SqlUnitOfWork):
    with pytest.raises(RuntimeError, match="outside uow.begin"):
        _ = uow.session

    with uow.begin():
        uow.session.add(models.Setting(key="k_ok", value="1"))
        with pytest.raises(RuntimeError, match="Nested"):
            with uow.begin():
                pass

    with pytest.raises(RuntimeError, match="outside uow.begin"):
        _ = uow.session

    with pytest.raises(ValueError, match="boom"):
        with uow.begin():
            uow.session.add(models.Setting(key="k_bad", value="2"))
            uow.flush()
            raise ValueError("boom")

    with uow.begin():
        assert uow.session.get(models.Setting, "k_ok") is not None
        assert uow.session.get(models.Setting, "k_bad") is None


@pytest.mark.asyncio
async def test_read_transaction_closes_session_and_returns_result(uow: SqlUnitOfWork):
    with uow.begin():
        uow.session.add(models.Setting(key="read_key", value="v1"))

    val = await read_transaction(
        uow, lambda uw: uw.session.get(models.Setting, "read_key").value
    )
    assert val == "v1"
    with pytest.raises(RuntimeError, match="outside uow.begin"):
        _ = uow.session


@pytest.mark.asyncio
async def test_run_write_transaction_retries_on_55p03_lock_timeout(committed_session_factory):
    # Seed one row in a committed transaction
    seed_uow = SqlUnitOfWork(committed_session_factory)
    with seed_uow.begin():
        seed_uow.session.add(models.Setting(key="lock_target", value="initial"))

    engine = committed_session_factory.kw["bind"]
    lock_acquired = threading.Event()
    release_lock = threading.Event()

    def _hold_row_lock() -> None:
        with engine.begin() as conn:
            conn.execute(
                text("SELECT value FROM setting WHERE key = 'lock_target' FOR UPDATE")
            )
            lock_acquired.set()
            release_lock.wait(timeout=5.0)

    holder = threading.Thread(target=_hold_row_lock, daemon=True)
    holder.start()
    assert lock_acquired.wait(timeout=3.0)

    # Start a background timer to release the lock after 180ms so attempt 1 hits 55P03
    # (with short lock_timeout) and a subsequent retry succeeds.
    def _release_soon() -> None:
        time.sleep(0.18)
        release_lock.set()

    releaser = threading.Thread(target=_release_soon, daemon=True)
    releaser.start()

    target_uow = SqlUnitOfWork(committed_session_factory)

    def _write_val(new_val: str):
        def _write(uw: SqlUnitOfWork) -> str:
            uw.session.execute(text("SET LOCAL lock_timeout = '40ms'"))
            row = uw.session.get(models.Setting, "lock_target")
            assert row is not None
            row.value = new_val
            return row.value
        return _write

    result = await run_write_transaction(
        target_uow, _write_val("updated"), max_attempts=6, base_delay=0.05, max_delay=0.1,
    )
    holder.join(timeout=2.0)
    releaser.join(timeout=2.0)
    assert result == "updated"

    # Also verify exhaustion raises OperationalError with 55P03
    lock_acquired2 = threading.Event()
    release_lock2 = threading.Event()

    def _hold_again() -> None:
        with engine.begin() as conn:
            conn.execute(
                text("SELECT value FROM setting WHERE key = 'lock_target' FOR UPDATE")
            )
            lock_acquired2.set()
            release_lock2.wait(timeout=5.0)

    holder2 = threading.Thread(target=_hold_again, daemon=True)
    holder2.start()
    assert lock_acquired2.wait(timeout=3.0)
    try:
        with pytest.raises(OperationalError):
            await run_write_transaction(
                SqlUnitOfWork(committed_session_factory),
                _write_val("updated_again"),
                max_attempts=2,
                base_delay=0.01,
                max_delay=0.02,
            )
    finally:
        release_lock2.set()
        holder2.join(timeout=2.0)
