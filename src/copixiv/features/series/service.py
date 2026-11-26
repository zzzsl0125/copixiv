"""Series application services: name/index resolution, dead-letter ledger, and repair."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import case, func
from sqlalchemy import delete as _delete
from sqlalchemy import select as _select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from copixiv.core.draft import NovelDraft, safe_get
from copixiv.core.models import TaskResult
from copixiv.db import models
from copixiv.db.tx import read_transaction, run_write_transaction
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.series.repo import SQLAlchemySeriesRepository
from copixiv.log import logger
from copixiv.pixiv.client import PixivClient
from copixiv.pixiv.errors import PixivApiError

RETIRE_AFTER_FAILURES = 3
DEFINITIVE_STATUSES = frozenset({404, 410})


# ---------------------------------------------------------------------------
# Dead-series ledger
# ---------------------------------------------------------------------------


def is_definitive_error(exc: BaseException | None) -> bool:
    """True when *exc* is pixiv definitively saying the series is gone."""
    if exc is None or not isinstance(exc, PixivApiError):
        return False
    return getattr(exc, "status_code", None) in DEFINITIVE_STATUSES


def _case_retire(prior_failures):
    return case(
        (prior_failures + 1 >= RETIRE_AFTER_FAILURES, func.now()),
        else_=models.SeriesDeadLetter.retired_at,
    )


def note_failure(session, series_id: int, exc: BaseException | None) -> bool:
    """Record one failed roster fetch; return True when this retires the series."""
    if not is_definitive_error(exc):
        return False

    status = getattr(exc, "status_code", None)
    detail = f"HTTP {status}: {exc}"
    now = datetime.now(UTC)

    stmt = (
        pg_insert(models.SeriesDeadLetter)
        .values(
            series_id=series_id,
            consecutive_failures=1,
            last_error=detail,
            last_failed_at=now,
        )
        .on_conflict_do_update(
            index_elements=[models.SeriesDeadLetter.series_id],
            set_={
                "consecutive_failures": (models.SeriesDeadLetter.consecutive_failures + 1),
                "last_error": detail,
                "last_failed_at": now,
                "retired_at": _case_retire(models.SeriesDeadLetter.consecutive_failures),
            },
        )
        .returning(models.SeriesDeadLetter.consecutive_failures)
    )
    count = int(session.execute(stmt).scalar_one())

    if count >= RETIRE_AFTER_FAILURES:
        logger.warning(
            f"fix_series_index: 系列 {series_id} 连续 {count} 轮 404，"
            "已判死并从候选集移除（恢复见 series_dead_letter 文档）"
        )
        return True
    return False


def retire_series(session, series_id: int, reason: str) -> None:
    """Immediately retire *series_id* in ``series_dead_letter`` (idempotent)."""
    now = datetime.now(UTC)
    stmt = (
        pg_insert(models.SeriesDeadLetter)
        .values(
            series_id=series_id,
            consecutive_failures=RETIRE_AFTER_FAILURES,
            last_error=reason,
            last_failed_at=now,
            retired_at=now,
        )
        .on_conflict_do_update(
            index_elements=[models.SeriesDeadLetter.series_id],
            set_={
                "consecutive_failures": RETIRE_AFTER_FAILURES,
                "last_error": reason,
                "last_failed_at": now,
                "retired_at": now,
            },
        )
    )
    session.execute(stmt)


def note_success(session, series_id: int) -> None:
    """Clear a series' ledger entry after a successful roster fetch."""
    session.execute(_delete(models.SeriesDeadLetter).where(models.SeriesDeadLetter.series_id == series_id))


def tracked_series_ids(session) -> set[int]:
    """Series IDs currently present in ``series_dead_letter``."""
    return set(session.execute(_select(models.SeriesDeadLetter.series_id)).scalars().all())


# ---------------------------------------------------------------------------
# Name resolution (read-only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeriesProfile:
    """Resolved series metadata alongside its lookup origin."""

    series_id: int
    series_name: str | None
    source: Literal["local", "pixiv", "unknown"]


async def lookup_series_profile(
    series_id: int,
    *,
    uow: SqlUnitOfWork,
    client: PixivClient | None,
) -> SeriesProfile:
    """Resolve one series id → name. Read-only, never raises."""
    local_name = await read_transaction(
        uow,
        lambda tx: SQLAlchemySeriesRepository(tx.session).get_name_fallback(series_id),
    )
    if local_name:
        return SeriesProfile(series_id, local_name, "local")

    name = await fetch_series_name(client, series_id)
    if name:
        return SeriesProfile(series_id, name, "pixiv")
    return SeriesProfile(series_id, None, "unknown")


async def fetch_series_name(
    client: PixivClient | None,
    series_id: int,
) -> str | None:
    """Ask Pixiv what one series is called, degrading to ``None`` on error."""
    if client is None or series_id <= 0:
        return None
    try:
        resp = await client.novel_series(series_id)
    except Exception as exc:  # noqa: BLE001 — any transport/auth error degrades
        logger.warning(f"Failed to fetch name for series #{series_id}: {exc}")
        return None

    name = safe_get(resp, "series.title") or safe_get(resp, "series_name")
    if name:
        return name
    for novel in safe_get(resp, "novels", []) or []:
        title = safe_get(novel, "series.title")
        if title:
            return title
    return None


# ---------------------------------------------------------------------------
# Ingest-time chapter index resolution
# ---------------------------------------------------------------------------


def _positions_by_series(drafts: list[NovelDraft]) -> dict[int, list[int]]:
    needs: dict[int, list[int]] = {}
    for index, draft in enumerate(drafts):
        if draft.series_id and draft.series_index is None:
            needs.setdefault(int(draft.series_id), []).append(index)
    return needs


def _authority_from_roster(roster: list) -> dict[int, int]:
    """``{novel_id: chapter_number}`` from a roster, 1-based by position."""
    authority: dict[int, int] = {}
    for pos0, item in enumerate(roster):
        item_id = safe_get(item, "id")
        if item_id is None:
            continue
        authority[int(item_id)] = pos0 + 1
    return authority


async def _fetch_ingest_roster(client, series_id: int) -> list | None:
    try:
        resp = await client.novel_series(series_id, fetch_all=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"series_index: 系列 {series_id} 名册请求失败（保留 NULL）: {exc}")
        return None
    novels = safe_get(resp, "novels", None)
    return list(novels) if novels else None


async def resolve_series_indices(drafts: list[NovelDraft], client) -> list[NovelDraft]:
    """Fill ``series_index`` for drafts that have a series but no index."""
    if client is None or not drafts:
        return drafts

    needs = _positions_by_series(drafts)
    if not needs:
        return drafts

    series_ids = list(needs)
    rosters = await asyncio.gather(
        *[_fetch_ingest_roster(client, sid) for sid in series_ids],
    )

    resolved = list(drafts)
    filled = 0
    for series_id, roster in zip(series_ids, rosters, strict=True):
        if not roster:
            continue
        authority = _authority_from_roster(roster)
        if not authority:
            continue
        for index in needs[series_id]:
            want = authority.get(drafts[index].id)
            if want is None:
                continue
            resolved[index] = replace(drafts[index], series_index=want)
            filled += 1

    if filled:
        logger.info(f"series_index: 下载阶段按名册补号 {filled} 本（涉及 {len(series_ids)} 个系列）")
    return resolved


# ---------------------------------------------------------------------------
# Maintenance chapter-index repair
# ---------------------------------------------------------------------------


def _page(ids: list[int], cap: int | None) -> tuple[list[int], bool]:
    if cap is None:
        return ids, False
    return ids[:cap], len(ids) > cap


async def run_series_repair(
    uow: SqlUnitOfWork,
    client: PixivClient,
    *,
    max_series: int | None = None,
) -> TaskResult:
    """Repair missing/mismatched ``series_index`` values using Pixiv series rosters."""
    single_book_ids = await read_transaction(
        uow,
        lambda uw: SQLAlchemySeriesRepository(uw.session).single_book_series_missing_index(),
    )
    if single_book_ids:
        await run_write_transaction(
            uow,
            lambda uw: SQLAlchemySeriesRepository(uw.session).update_series_indices(
                {nid: 1 for nid in single_book_ids}
            ),
        )
        logger.info(
            f"fix_series_index: 单本系列直接标 1 号 {len(single_book_ids)} 本"
            f"（免去 {len(single_book_ids)} 次名册请求）"
        )

    def _read_candidates(uw: SqlUnitOfWork) -> tuple[set[int], list[int]]:
        dl_ids = tracked_series_ids(uw.session)
        cands = sorted(SQLAlchemySeriesRepository(uw.session).series_with_empty_index())
        return dl_ids, cands

    dead_letter_ids, candidates = await read_transaction(uow, _read_candidates)
    series_page, series_truncated = _page(candidates, max_series)

    if not series_page:
        if single_book_ids:
            return TaskResult(summary=f"系列章节号修复: 单本系列补号 {len(single_book_ids)} 本")
        return TaskResult(summary="系列章节号检查: 无需修复")

    stats = {
        "filled": 0,
        "fixed": 0,
        "series_done": 0,
        "failed": 0,
    }
    new_roster_absent_ids: set[int] = set()

    async def _note_roster_failure(series_id: int, exc: BaseException) -> None:
        try:
            await run_write_transaction(uow, lambda uw: note_failure(uw.session, series_id, exc))
        except Exception:  # noqa: BLE001
            logger.exception(f"fix_series_index: 系列 {series_id} 台账写入失败（不影响本轮）")

    async def _note_roster_success(series_id: int) -> None:
        try:
            await run_write_transaction(uow, lambda uw: note_success(uw.session, series_id))
        except Exception:  # noqa: BLE001
            logger.exception(f"fix_series_index: 系列 {series_id} 台账清理失败（不影响本轮）")

    async def _fetch_roster(series_id: int) -> list | None:
        try:
            resp = await client.novel_series(series_id, fetch_all=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"fix_series_index: 系列 {series_id} 名册请求失败: {exc}")
            await _note_roster_failure(series_id, exc)
            return None

        novels = safe_get(resp, "novels", None)
        roster = list(novels) if novels else None
        if (roster is not None or novels is not None) and series_id in dead_letter_ids:
            await _note_roster_success(series_id)
            dead_letter_ids.discard(series_id)
        return roster

    async def _apply_roster(series_id: int, roster: list) -> tuple[int, int]:
        authority = _authority_from_roster(roster)
        stored = await read_transaction(
            uow,
            lambda uw: SQLAlchemySeriesRepository(uw.session).novels_of_series(series_id),
        )

        fills: dict[int, int] = {}
        fixes: dict[int, int] = {}
        has_unresolvable_missing = False
        for columns in stored:
            novel_id = int(columns["id"])
            want = authority.get(novel_id)
            if want is None or not columns.get("author_id"):
                if columns["series_index"] is None:
                    has_unresolvable_missing = True
                continue
            if columns["series_index"] is None:
                fills[novel_id] = want
            elif int(columns["series_index"]) != want:
                fixes[novel_id] = want

        if has_unresolvable_missing:
            new_roster_absent_ids.add(series_id)

        if fills or fixes:
            index_updates: dict[int, int | None] = {**fills, **fixes}
            await run_write_transaction(
                uow,
                lambda uw: SQLAlchemySeriesRepository(uw.session).update_series_indices(index_updates),
            )

        return len(fills), len(fixes)

    for series_id in series_page:
        roster = await _fetch_roster(series_id)
        if roster is None:
            stats["failed"] += 1
            continue
        filled, fixed = await _apply_roster(series_id, roster)
        stats["filled"] += filled
        stats["fixed"] += fixed
        stats["series_done"] += 1

    if new_roster_absent_ids:

        def _retire_absent(uw: SqlUnitOfWork) -> None:
            for sid in sorted(new_roster_absent_ids):
                retire_series(uw.session, sid, "roster_absent")

        await run_write_transaction(uow, _retire_absent)

    if stats["series_done"] == 0:
        return TaskResult(summary="系列章节号检查: API 请求全部失败")

    summary = (
        f"系列章节号修复: 补号 {stats['filled']} 本, 纠正 {stats['fixed']} 本 "
        f"(系列 {stats['series_done']}, 失败 {stats['failed']})"
    )
    if series_truncated:
        summary += "；本轮达上限，下轮续跑"
    return TaskResult(summary=summary)


__all__ = [
    "DEFINITIVE_STATUSES",
    "RETIRE_AFTER_FAILURES",
    "SeriesProfile",
    "fetch_series_name",
    "is_definitive_error",
    "lookup_series_profile",
    "note_failure",
    "note_success",
    "resolve_series_indices",
    "retire_series",
    "run_series_repair",
    "tracked_series_ids",
]
