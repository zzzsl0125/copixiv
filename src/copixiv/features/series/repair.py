"""Series chapter-index and orphan-row repair use case."""

from __future__ import annotations

import json

from copixiv.core.draft import guess_series_order, safe_get
from copixiv.core.models import TaskResult
from copixiv.db.tx import read_transaction, run_write_transaction
from copixiv.db.uow import SqlUnitOfWork
from copixiv.features.series import ledger as series_ledger
from copixiv.features.series.repo import SQLAlchemySeriesRepository
from copixiv.features.system.repo import SQLAlchemySettingRepository
from copixiv.log import logger
from copixiv.pixiv.client import PixivClient

_SERIES_CURSOR_KEY = "fix_series_index.cursor"
_ORPHAN_CURSOR_KEY = "fix_series_index.orphan_cursor"
_SWEEP_CURSOR_KEY = "fix_series_index.sweep_cursor"
_ROSTER_ABSENT_KEY = "fix_series_index.roster_absent"


def _load_roster_absent(settings: SQLAlchemySettingRepository) -> set[int]:
    raw = settings.get_value(_ROSTER_ABSENT_KEY)
    if not raw:
        return set()
    try:
        items = json.loads(raw)
    except ValueError:
        return set()
    if not isinstance(items, list):
        return set()
    return {int(x) for x in items if isinstance(x, int) or (isinstance(x, str) and x.isdigit())}


def _page(ids: list[int], cap: int | None) -> tuple[list[int], bool]:
    if cap is None:
        return ids, False
    return ids[:cap], len(ids) > cap


async def _stored_columns(uow: SqlUnitOfWork, novel_id: int) -> dict | None:
    return await read_transaction(
        uow, lambda uw: SQLAlchemySeriesRepository(uw.session).novel_columns(novel_id)
    )


async def run_series_repair(
    uow: SqlUnitOfWork,
    client: PixivClient,
    *,
    max_series: int | None = None,
    max_orphans: int | None = None,
    max_sweep: int | None = None,
    sweep_all: bool = False,
) -> TaskResult:
    """Repair missing/mismatched ``series_index`` values and orphan ``series_id`` rows."""
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

    def _read_candidates(
        uw,
    ) -> tuple[int, int, int, set[int], set[int], list[int], list, list[int]]:
        settings = SQLAlchemySettingRepository(uw.session)
        s_cursor = settings.get_int(_SERIES_CURSOR_KEY, 0)
        o_cursor = settings.get_int(_ORPHAN_CURSOR_KEY, 0)
        w_cursor = settings.get_int(_SWEEP_CURSOR_KEY, 0)
        absent_ids = _load_roster_absent(settings)
        dl_ids = series_ledger.tracked_series_ids(uw.session)

        repo = SQLAlchemySeriesRepository(uw.session)
        cands = sorted(sid for sid in repo.series_with_empty_index() if sid not in absent_ids)
        orphans = repo.novels_with_orphan_index(
            limit=(max_orphans + 1 if max_orphans is not None else None),
            after_id=o_cursor,
        )
        sweeps = (
            repo.series_with_stored_index(
                after_id=w_cursor,
                limit=(max_sweep + 1 if max_sweep is not None else None),
            )
            if sweep_all
            else []
        )
        return s_cursor, o_cursor, w_cursor, absent_ids, dl_ids, cands, orphans, sweeps

    (
        series_cursor,
        orphan_cursor,
        sweep_cursor,
        roster_absent_ids,
        dead_letter_ids,
        candidates,
        orphan_candidates,
        sweep_candidates,
    ) = await read_transaction(uow, _read_candidates)

    series_page, series_truncated = _page([sid for sid in candidates if sid > series_cursor], max_series)
    orphan_truncated = max_orphans is not None and len(orphan_candidates) > max_orphans
    orphan_page = orphan_candidates[:max_orphans] if max_orphans is not None else orphan_candidates
    sweep_truncated = max_sweep is not None and len(sweep_candidates) > max_sweep
    sweep_page = sweep_candidates[:max_sweep] if max_sweep is not None else sweep_candidates

    if not series_page and not orphan_page and not sweep_page:
        reset_keys = [
            key
            for key, val in (
                (_SERIES_CURSOR_KEY, series_cursor),
                (_ORPHAN_CURSOR_KEY, orphan_cursor),
                (_SWEEP_CURSOR_KEY, sweep_cursor),
            )
            if val
        ]
        if reset_keys:

            def _reset_cursors(uw: SqlUnitOfWork) -> None:
                settings = SQLAlchemySettingRepository(uw.session)
                for key in reset_keys:
                    settings.set_int(key, 0)

            await run_write_transaction(uow, _reset_cursors)
        return TaskResult(summary="系列章节号检查: 无需修复")

    stats = {
        "filled": 0,
        "fixed": 0,
        "glued": 0,
        "cleared": 0,
        "series_done": 0,
        "orphan_done": 0,
        "sweep_done": 0,
        "failed": 0,
    }
    rosters: dict[int, list | None] = {}
    new_roster_absent_ids: set[int] = set()
    cursor_updates: dict[str, int] = {}

    async def _note_roster_failure(series_id: int, exc: BaseException) -> None:
        try:
            await run_write_transaction(
                uow, lambda uw: series_ledger.note_failure(uw.session, series_id, exc)
            )
        except Exception:  # noqa: BLE001
            logger.exception(f"fix_series_index: 系列 {series_id} 台账写入失败（不影响本轮）")

    async def _note_roster_success(series_id: int) -> None:
        try:
            await run_write_transaction(uow, lambda uw: series_ledger.note_success(uw.session, series_id))
        except Exception:  # noqa: BLE001
            logger.exception(f"fix_series_index: 系列 {series_id} 台账清理失败（不影响本轮）")

    async def _roster(series_id: int) -> list | None:
        if series_id not in rosters:
            try:
                resp = await client.novel_series(series_id, fetch_all=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"fix_series_index: 系列 {series_id} 名册请求失败: {exc}")
                rosters[series_id] = None
                await _note_roster_failure(series_id, exc)
            else:
                novels = safe_get(resp, "novels", None)
                rosters[series_id] = list(novels) if novels else None
                if (rosters[series_id] is not None or novels is not None) and series_id in dead_letter_ids:
                    await _note_roster_success(series_id)
                    dead_letter_ids.discard(series_id)
        return rosters[series_id]

    async def _apply_roster(
        series_id: int,
        roster: list,
        forced: dict[int, tuple[dict, str | None, int | None]],
    ) -> tuple[int, int, set[int]]:
        authority: dict[int, int] = {}
        for pos0, item in enumerate(roster):
            item_id = safe_get(item, "id")
            if item_id is not None:
                authority[int(item_id)] = pos0 + 1

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

        glue_items: list[tuple[int, dict, str | None, int | None, int]] = []
        for novel_id, (columns, series_name, author_id) in forced.items():
            want = authority.get(novel_id)
            if want is None:
                continue
            if not (columns.get("author_id") or author_id):
                continue
            glue_items.append((novel_id, columns, series_name, author_id, want))

        glued: set[int] = set()
        if fills or fixes or glue_items:
            index_updates: dict[int, int | None] = {**fills, **fixes}

            def _write(uw) -> None:
                repo = SQLAlchemySeriesRepository(uw.session)
                if index_updates:
                    repo.update_series_indices(index_updates)
                for nid, columns, series_name, author_id, want in glue_items:
                    if repo.glue_orphan_novel(
                        nid,
                        series_id=series_id,
                        series_name=series_name or columns.get("series_name"),
                        series_index=want,
                        author_id=author_id,
                        force_series_index=True,
                    ):
                        glued.add(nid)

            await run_write_transaction(uow, _write)

        return len(fills), len(fixes), glued

    async def _write_glue_single(
        columns: dict,
        series_id: int,
        series_name: str | None,
        index: int | None,
        author_id: int | None,
    ) -> bool:
        novel_id = int(columns["id"])
        effective_author = columns.get("author_id") or author_id
        if not effective_author:
            return False

        return await run_write_transaction(
            uow,
            lambda uw: SQLAlchemySeriesRepository(uw.session).glue_orphan_novel(
                novel_id,
                series_id=int(series_id),
                series_name=series_name or columns.get("series_name"),
                series_index=index,
                author_id=effective_author,
                force_series_index=bool(index),
            ),
        )

    async def _glue_orphan(
        novel_id: int,
        series_id: int,
        columns: dict,
        data,
        series_name: str | None,
        author_id: int | None,
    ) -> tuple[bool, int, int]:
        siblings = await read_transaction(
            uow,
            lambda uw: SQLAlchemySeriesRepository(uw.session).count_novels_by_series(series_id),
        )
        nav_index = guess_series_order(safe_get(data, "series_navigation"))
        title = safe_get(data, "series_title") or series_name
        if not siblings and nav_index:
            ok = await _write_glue_single(columns, series_id, title, nav_index, author_id)
            return ok, 0, 0

        roster = await _roster(series_id)
        if roster is None:
            return False, 0, 0
        if not any(int(safe_get(item, "id", 0) or 0) == novel_id for item in roster):
            ok = await _write_glue_single(columns, series_id, title, nav_index, author_id)
            return ok, 0, 0

        filled, fixed, glued = await _apply_roster(series_id, roster, {novel_id: (columns, title, author_id)})
        return novel_id in glued, filled, fixed

    async def _clear_orphan_index(columns: dict) -> bool:
        await run_write_transaction(
            uow,
            lambda uw: SQLAlchemySeriesRepository(uw.session).update_series_indices(
                {int(columns["id"]): None}
            ),
        )
        return True

    async def _run_roster_branch(
        page: list[int], cursor: int, key: str, counter: str, truncated: bool
    ) -> None:
        next_cursor = cursor
        for series_id in page:
            next_cursor = series_id
            roster = await _roster(series_id)
            if roster is None:
                stats["failed"] += 1
            else:
                filled, fixed, _ = await _apply_roster(series_id, roster, {})
                stats["filled"] += filled
                stats["fixed"] += fixed
                stats[counter] += 1

        if not truncated and (cursor or page):
            cursor_updates[key] = 0
        elif page:
            cursor_updates[key] = next_cursor

    await _run_roster_branch(series_page, series_cursor, _SERIES_CURSOR_KEY, "series_done", series_truncated)

    orphan_next = orphan_cursor
    for novel_id, series_name, _index in orphan_page:
        orphan_next = novel_id
        columns = await _stored_columns(uow, novel_id)
        if columns is None:
            stats["failed"] += 1
            continue
        try:
            data = await client.webview_novel(novel_id)
        except Exception as exc:  # noqa: BLE001
            if series_ledger.is_definitive_error(exc):
                logger.info(f"fix_series_index: #{novel_id} 已在 Pixiv 删除 ({exc})，清除残留 series_index")
                await _clear_orphan_index(columns)
                stats["cleared"] += 1
                stats["orphan_done"] += 1
                continue
            logger.warning(f"fix_series_index: #{novel_id} webview 请求失败: {exc}")
            data = None
        if not data:
            stats["failed"] += 1
            continue

        series_id = safe_get(data, "series_id")
        author_id = safe_get(data, "user_id")
        if series_id:
            ok, filled, fixed = await _glue_orphan(
                novel_id, int(series_id), columns, data, series_name, author_id
            )
            stats["filled"] += filled
            stats["fixed"] += fixed
            stats["glued"] += 1 if ok else 0
        else:
            ok = await _clear_orphan_index(columns)
            stats["cleared"] += 1 if ok else 0
        if not ok:
            stats["failed"] += 1
        else:
            stats["orphan_done"] += 1

    if not orphan_truncated and (orphan_cursor or orphan_page):
        cursor_updates[_ORPHAN_CURSOR_KEY] = 0
    elif orphan_page:
        cursor_updates[_ORPHAN_CURSOR_KEY] = orphan_next

    if sweep_all:
        await _run_roster_branch(sweep_page, sweep_cursor, _SWEEP_CURSOR_KEY, "sweep_done", sweep_truncated)

    if cursor_updates or new_roster_absent_ids:

        def _persist_round_state(uw: SqlUnitOfWork) -> None:
            settings = SQLAlchemySettingRepository(uw.session)
            for key, value in cursor_updates.items():
                settings.set_int(key, value)
            if new_roster_absent_ids:
                updated = sorted(roster_absent_ids | new_roster_absent_ids)
                settings.set_value(_ROSTER_ABSENT_KEY, json.dumps(updated))

        await run_write_transaction(uow, _persist_round_state)

    if stats["series_done"] == 0 and stats["orphan_done"] == 0 and stats["sweep_done"] == 0:
        return TaskResult(summary="系列章节号检查: API 请求全部失败")

    summary = (
        f"系列章节号修复: 补号 {stats['filled']} 本, 纠正 {stats['fixed']} 本, "
        f"孤儿挂回 {stats['glued']} 本, 孤儿清号 {stats['cleared']} 本 "
        f"(系列 {stats['series_done']}, 孤儿 {stats['orphan_done']}, "
        f"复查 {stats['sweep_done']}, 失败 {stats['failed']})"
    )
    if series_truncated or orphan_truncated or sweep_truncated:
        summary += "；本轮达上限，下轮续跑"
    return TaskResult(summary=summary)


__all__ = [
    "run_series_repair",
]
