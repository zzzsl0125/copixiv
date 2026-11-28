#!/usr/bin/env python3
"""Migrate legacy ``download/`` files into the equal multi-source directory layout.

Default mode is ``--dry-run`` (read-only analysis and report). Pass ``--apply``
to move files on disk (`.txt`, `.epub`, and associated cover/illustration assets)
and update ``novel.path`` in PostgreSQL in batches.

Usage::

    .venv/bin/python scripts/migrate_download_layout.py
    .venv/bin/python scripts/migrate_download_layout.py --sample 15
    .venv/bin/python scripts/migrate_download_layout.py --apply --batch-size 500
"""

from __future__ import annotations

import argparse
import errno
import os
import shutil
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from sqlalchemy import bindparam, select, update
from sqlalchemy.engine import Engine

from copixiv.config import load_config
from copixiv.core import naming as formats
from copixiv.db.engine import create_engine
from copixiv.db.models import Novel, Series
from copixiv.storage import assets
from copixiv.storage.atomic import resolve_within_root
from copixiv.storage.file_storage import FileStorage

MoveAction = Literal[
    "to_move",
    "already_canonical",
    "db_only_resume",
    "missing_on_disk",
    "conflict",
    "outside_root",
]


@dataclass(frozen=True)
class NovelMigrationRow:
    """Minimal database projection required to plan a novel's disk migration."""

    id: int
    title: str
    source: str
    series_id: int | None
    novel_series_name: str | None
    series_index: int | None
    path: str | None
    has_epub: int
    series_name: str | None = None
    series_source_key: str | None = None


@dataclass(frozen=True)
class PlannedMove:
    """Planned filesystem + DB migration action for one novel row."""

    novel_id: int
    source: str
    is_series: bool
    action: MoveAction
    old_rel_path: str
    new_rel_path: str
    old_txt: Path | None
    new_txt: Path | None
    old_epub: Path | None
    new_epub: Path | None
    asset_moves: tuple[tuple[Path, Path], ...] = ()
    reason: str = ""


@dataclass
class MigrationPlan:
    """Complete migration plan and summary statistics across all queried rows."""

    download_dir: str
    root: Path
    moves: list[PlannedMove] = field(default_factory=list)
    action_counts: Counter[str] = field(default_factory=Counter)
    bucket_counts: Counter[tuple[str, str, str]] = field(default_factory=Counter)
    txt_move_count: int = 0
    epub_move_count: int = 0
    asset_move_count: int = 0
    series_dirs_to_create: set[Path] = field(default_factory=set)
    candidate_empty_dirs: set[Path] = field(default_factory=set)
    dirs_becoming_empty: set[Path] = field(default_factory=set)
    dirs_with_leftovers: dict[Path, list[str]] = field(default_factory=dict)
    root_unmanaged_files: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MigrationApplyResult:
    """Execution summary returned by :func:`apply_migration_plan`."""

    attempted_rows: int
    updated_db_rows: int
    moved_txt: int
    moved_epub: int
    moved_assets: int
    removed_empty_dirs: int
    failed_rows: list[tuple[int, str]]


def load_migration_rows(engine: Engine, *, source: str | None = None) -> list[NovelMigrationRow]:
    """Load all novel rows paired with their series metadata ordered by ID."""
    stmt = (
        select(
            Novel.id,
            Novel.title,
            Novel.source,
            Novel.series_id,
            Novel.series_name,
            Novel.series_index,
            Novel.path,
            Novel.has_epub,
            Series.series_name,
            Series.source_key,
        )
        .select_from(Novel)
        .outerjoin(Series, Series.series_id == Novel.series_id)
        .order_by(Novel.id.asc())
    )
    if source is not None:
        stmt = stmt.where(Novel.source == source)

    with engine.connect() as conn:
        return [
            NovelMigrationRow(
                id=int(r[0]),
                title=str(r[1] or ""),
                source=str(r[2] or "pixiv"),
                series_id=int(r[3]) if r[3] is not None else None,
                novel_series_name=r[4],
                series_index=int(r[5]) if r[5] is not None else None,
                path=r[6],
                has_epub=int(r[7] or 0),
                series_name=r[8],
                series_source_key=r[9],
            )
            for r in conn.execute(stmt)
        ]


def _chapter_sort_key(row: NovelMigrationRow) -> tuple[bool, int, int, int]:
    return (row.series_index is None, row.series_index or 0, abs(row.id), row.id)


def resolve_series_dirs(
    rows: list[NovelMigrationRow],
    download_dir: str,
    file_storage: FileStorage,
) -> dict[int, Path]:
    """Resolve one canonical series directory per ``series_id``."""
    grouped: dict[int, list[NovelMigrationRow]] = defaultdict(list)
    for row in rows:
        if row.series_id is not None:
            grouped[row.series_id].append(row)

    result: dict[int, Path] = {}
    for series_id, chapters in grouped.items():
        ordered = sorted(chapters, key=_chapter_sort_key)
        first = ordered[0]
        existing = file_storage.find_existing_series_dir((c.path for c in ordered), first.source)
        if existing is not None:
            result[series_id] = existing
            continue

        canonical_name = (
            (first.series_name or "").strip()
            or next(
                (
                    c.novel_series_name.strip()
                    for c in ordered
                    if c.novel_series_name and c.novel_series_name.strip()
                ),
                "",
            )
            or first.title.strip()
            or f"series_{series_id}"
        )
        series_key = (first.series_source_key or "").strip() or str(series_id)
        sample_target = formats.build_path(
            first.id,
            first.title,
            download_dir,
            source=first.source,
            series_id=series_id,
            series_name=canonical_name,
            series_key=series_key,
        )
        result[series_id] = Path(sample_target).parent

    return result


@dataclass
class _DirSnapshot:
    files: set[str]
    has_subdirs: bool
    assets_by_novel: dict[int, list[Path]]


def _scan_directory(directory: Path) -> _DirSnapshot:
    files: set[str] = set()
    has_subdirs = False
    assets_by_novel: dict[int, list[Path]] = defaultdict(list)
    try:
        with os.scandir(directory) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        has_subdirs = True
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                files.add(entry.name)
                entry_path = Path(entry.path)
                if entry_path.suffix.lower() in assets.IMAGE_SUFFIXES:
                    nid = assets.novel_id_of(entry_path.stem)
                    if nid is not None:
                        assets_by_novel[nid].append(entry_path)
    except OSError:
        pass
    return _DirSnapshot(files=files, has_subdirs=has_subdirs, assets_by_novel=assets_by_novel)


def _describe_old_bucket(old_path: str, download_dir: str) -> str:
    prefix = download_dir.rstrip("/") + "/"
    rel = old_path[len(prefix) :] if old_path.startswith(prefix) else old_path
    parts = Path(rel).parts
    if len(parts) <= 1:
        return f"{Path(download_dir).name}/ (根目录散落)"
    top = parts[0]
    if len(top) == 4 and top.isdigit():
        return f"{Path(download_dir).name}/{{4位分片}}/ (旧 Pixiv)"
    if top == "pixiv":
        return f"{Path(download_dir).name}/pixiv/..."
    if top == "local":
        return f"{Path(download_dir).name}/local/..."
    if len(parts) == 2:
        return f"{Path(download_dir).name}/{top}/ (旧扁平目录)"
    return f"{Path(download_dir).name}/{top}/..."


def build_migration_plan(
    rows: list[NovelMigrationRow],
    download_dir: str = "download",
) -> MigrationPlan:
    """Build a read-only filesystem and DB migration plan without mutating disk or DB."""
    file_storage = FileStorage(download_dir)
    root = Path(download_dir).resolve()
    series_dirs = resolve_series_dirs(rows, download_dir, file_storage)

    dir_cache: dict[Path, _DirSnapshot] = {}

    def get_snapshot(directory: Path) -> _DirSnapshot:
        resolved = directory.resolve()
        snap = dir_cache.get(resolved)
        if snap is None:
            snap = _scan_directory(resolved) if resolved.is_dir() else _DirSnapshot(set(), False, {})
            dir_cache[resolved] = snap
        return snap

    plan = MigrationPlan(download_dir=download_dir, root=root)
    seen_targets: set[Path] = set()
    moved_files_by_dir: dict[Path, set[str]] = defaultdict(set)
    target_dirs_receiving: set[Path] = set()

    for row in rows:
        is_series = row.series_id is not None
        raw_path = (row.path or "").strip()
        if not raw_path:
            plan.moves.append(
                PlannedMove(
                    novel_id=row.id,
                    source=row.source,
                    is_series=is_series,
                    action="outside_root",
                    old_rel_path="",
                    new_rel_path="",
                    old_txt=None,
                    new_txt=None,
                    old_epub=None,
                    new_epub=None,
                    reason="novel.path 为空",
                )
            )
            plan.action_counts["outside_root"] += 1
            continue

        src_txt = resolve_within_root(Path(raw_path), root)
        if src_txt is None:
            plan.moves.append(
                PlannedMove(
                    novel_id=row.id,
                    source=row.source,
                    is_series=is_series,
                    action="outside_root",
                    old_rel_path=raw_path,
                    new_rel_path="",
                    old_txt=None,
                    new_txt=None,
                    old_epub=None,
                    new_epub=None,
                    reason="路径落在 download 根目录之外",
                )
            )
            plan.action_counts["outside_root"] += 1
            continue

        series_dir = series_dirs.get(row.series_id) if row.series_id is not None else None
        series_name = (row.series_name or "").strip() or (row.novel_series_name or "").strip() or None
        series_key = (row.series_source_key or "").strip() or (
            str(row.series_id) if row.series_id is not None else None
        )
        new_rel_path = formats.build_path(
            row.id,
            row.title,
            download_dir,
            source=row.source,
            series_id=row.series_id,
            series_name=series_name,
            series_key=series_key,
            series_index=row.series_index,
            series_dir=series_dir,
        )
        dst_txt = (root / Path(new_rel_path).relative_to(Path(download_dir))).resolve()
        if dst_txt.parent == src_txt.parent and (
            dst_txt == src_txt or not is_series or row.series_id is not None
        ):
            # Already sits in the exact target directory (preserves stored title anchors).
            plan.moves.append(
                PlannedMove(
                    novel_id=row.id,
                    source=row.source,
                    is_series=is_series,
                    action="already_canonical",
                    old_rel_path=raw_path,
                    new_rel_path=raw_path,
                    old_txt=src_txt,
                    new_txt=src_txt,
                    old_epub=None,
                    new_epub=None,
                )
            )
            plan.action_counts["already_canonical"] += 1
            plan.bucket_counts[
                (row.source, _describe_old_bucket(raw_path, download_dir), "already_canonical")
            ] += 1
            continue

        src_dir = src_txt.parent
        dst_dir = dst_txt.parent
        src_snap = get_snapshot(src_dir)
        dst_snap = get_snapshot(dst_dir)

        src_epub = formats.sibling_path(src_txt, formats.EPUB)
        dst_epub = formats.sibling_path(dst_txt, formats.EPUB)

        src_txt_exists = src_txt.name in src_snap.files
        dst_txt_exists = dst_txt.name in dst_snap.files
        src_epub_exists = src_epub.name in src_snap.files
        dst_epub_exists = dst_epub.name in dst_snap.files
        row_assets = sorted(src_snap.assets_by_novel.get(row.id, ()))

        conflict_reason: str | None = None
        asset_moves: list[tuple[Path, Path]] = []
        if dst_txt in seen_targets:
            conflict_reason = "多个条目的目标路径冲突"
        elif src_txt_exists and dst_txt_exists:
            conflict_reason = "源 TXT 与目标 TXT 同时存在"
        elif src_epub_exists and dst_epub_exists:
            conflict_reason = "源 EPUB 与目标 EPUB 同时存在"
        else:
            for src_asset in row_assets:
                dst_asset = dst_dir / src_asset.name
                if dst_asset.name in dst_snap.files or dst_asset in seen_targets:
                    conflict_reason = f"图片资产目标冲突: {src_asset.name}"
                    break
                asset_moves.append((src_asset, dst_asset))

        if conflict_reason is not None:
            plan.moves.append(
                PlannedMove(
                    novel_id=row.id,
                    source=row.source,
                    is_series=is_series,
                    action="conflict",
                    old_rel_path=raw_path,
                    new_rel_path=new_rel_path,
                    old_txt=src_txt,
                    new_txt=dst_txt,
                    old_epub=src_epub if src_epub_exists else None,
                    new_epub=dst_epub if src_epub_exists else None,
                    reason=conflict_reason,
                )
            )
            plan.action_counts["conflict"] += 1
            plan.bucket_counts[(row.source, _describe_old_bucket(raw_path, download_dir), "conflict")] += 1
            continue

        is_resume = not src_txt_exists and dst_txt_exists
        if not is_resume and not src_txt_exists and not src_epub_exists and not asset_moves:
            plan.moves.append(
                PlannedMove(
                    novel_id=row.id,
                    source=row.source,
                    is_series=is_series,
                    action="missing_on_disk",
                    old_rel_path=raw_path,
                    new_rel_path=new_rel_path,
                    old_txt=src_txt,
                    new_txt=dst_txt,
                    old_epub=None,
                    new_epub=None,
                    reason="磁盘上未找到 TXT / EPUB / 图片文件",
                )
            )
            plan.action_counts["missing_on_disk"] += 1
            plan.bucket_counts[
                (row.source, _describe_old_bucket(raw_path, download_dir), "missing_on_disk")
            ] += 1
            continue

        action_name: MoveAction = "db_only_resume" if is_resume else "to_move"
        seen_targets.add(dst_txt)
        for _src_asset, dst_asset in asset_moves:
            seen_targets.add(dst_asset)

        txt_pair = (
            (src_txt, dst_txt)
            if (not is_resume and src_txt_exists)
            else (None, dst_txt if is_resume else None)
        )
        epub_pair = (src_epub, dst_epub) if src_epub_exists else (None, None)

        if txt_pair[0] is not None:
            moved_files_by_dir[src_dir].add(txt_pair[0].name)
            plan.txt_move_count += 1
        if epub_pair[0] is not None:
            moved_files_by_dir[src_dir].add(epub_pair[0].name)
            plan.epub_move_count += 1
        for src_asset, _dst_asset in asset_moves:
            moved_files_by_dir[src_dir].add(src_asset.name)
        plan.asset_move_count += len(asset_moves)

        if is_series and not dst_dir.exists():
            plan.series_dirs_to_create.add(dst_dir)
        target_dirs_receiving.add(dst_dir)
        if src_dir != root:
            plan.candidate_empty_dirs.add(src_dir)

        plan.moves.append(
            PlannedMove(
                novel_id=row.id,
                source=row.source,
                is_series=is_series,
                action=action_name,
                old_rel_path=raw_path,
                new_rel_path=new_rel_path,
                old_txt=txt_pair[0],
                new_txt=txt_pair[1],
                old_epub=epub_pair[0],
                new_epub=epub_pair[1],
                asset_moves=tuple(asset_moves),
                reason="目标 TXT 已存在，仅补齐 DB path 与剩余附属文件" if is_resume else "",
            )
        )
        plan.action_counts[action_name] += 1
        plan.bucket_counts[(row.source, _describe_old_bucket(raw_path, download_dir), action_name)] += 1

    # Inspect which legacy directories will become empty vs retain leftovers.
    target_ancestors: set[Path] = set()
    for tdir in target_dirs_receiving:
        cur = tdir.parent
        while cur != root and resolve_within_root(cur, root) is not None:
            if cur in target_ancestors:
                break
            target_ancestors.add(cur)
            cur = cur.parent

    for cand_dir in sorted(plan.candidate_empty_dirs):
        if cand_dir in target_dirs_receiving or cand_dir in target_ancestors:
            continue
        snap = get_snapshot(cand_dir)
        remaining = sorted(snap.files - moved_files_by_dir.get(cand_dir, set()))
        if not remaining and not snap.has_subdirs:
            plan.dirs_becoming_empty.add(cand_dir)
        elif remaining:
            plan.dirs_with_leftovers[cand_dir] = remaining

    if root.is_dir():
        root_snap = get_snapshot(root)
        plan.root_unmanaged_files = sorted(root_snap.files - moved_files_by_dir.get(root, set()))

    return plan


def _move_file_safe(src: Path, dst: Path) -> None:
    if dst.exists():
        raise FileExistsError(f"目标文件已存在，拒绝覆盖: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        shutil.move(str(src), str(dst))


def apply_migration_plan(
    plan: MigrationPlan,
    engine: Engine,
    *,
    batch_size: int = 500,
) -> MigrationApplyResult:
    """Execute planned file moves and batch-update ``novel.path`` in PostgreSQL."""
    actionable = [m for m in plan.moves if m.action in ("to_move", "db_only_resume")]
    moved_txt = 0
    moved_epub = 0
    moved_assets = 0
    updated_db_rows = 0
    failed_rows: list[tuple[int, str]] = []

    update_stmt = update(Novel).where(Novel.id == bindparam("b_id")).values(path=bindparam("b_path"))

    for start in range(0, len(actionable), max(1, batch_size)):
        chunk = actionable[start : start + max(1, batch_size)]
        db_params: list[dict[str, object]] = []

        for item in chunk:
            try:
                if item.old_txt is not None and item.new_txt is not None:
                    _move_file_safe(item.old_txt, item.new_txt)
                    moved_txt += 1
                if item.old_epub is not None and item.new_epub is not None:
                    _move_file_safe(item.old_epub, item.new_epub)
                    moved_epub += 1
                for src_asset, dst_asset in item.asset_moves:
                    _move_file_safe(src_asset, dst_asset)
                    moved_assets += 1
                db_params.append({"b_id": item.novel_id, "b_path": item.new_rel_path})
            except OSError as exc:
                failed_rows.append((item.novel_id, str(exc)))

        if db_params:
            with engine.begin() as conn:
                conn.execute(update_stmt, db_params)
            updated_db_rows += len(db_params)

    removed_empty_dirs = 0
    # Deepest-first so emptied nested directories are removed cleanly up to download root.
    for cand_dir in sorted(plan.candidate_empty_dirs, key=lambda p: len(p.parts), reverse=True):
        current = cand_dir.resolve()
        while current != plan.root and resolve_within_root(current, plan.root) is not None:
            try:
                if any(current.iterdir()):
                    break
                current.rmdir()
                removed_empty_dirs += 1
            except OSError:
                break
            current = current.parent

    return MigrationApplyResult(
        attempted_rows=len(actionable),
        updated_db_rows=updated_db_rows,
        moved_txt=moved_txt,
        moved_epub=moved_epub,
        moved_assets=moved_assets,
        removed_empty_dirs=removed_empty_dirs,
        failed_rows=failed_rows,
    )


def format_report(
    plan: MigrationPlan, *, sample_limit: int = 10, applied: MigrationApplyResult | None = None
) -> str:
    """Format a human-readable migration summary and sample listing."""
    lines: list[str] = []
    mode_label = (
        "执行模式 (--apply)" if applied is not None else "试运行预览 (--dry-run，未修改任何文件或数据库)"
    )
    lines.append(f"=== download 目录布局迁移报告 [{mode_label}] ===")
    lines.append(f"下载根目录: {plan.root}（配置项: {plan.download_dir}）")
    lines.append(f"扫描小说总行数: {len(plan.moves)}")
    lines.append("")
    lines.append("【状态汇总】")
    for key, label in (
        ("to_move", "待搬迁文件并更新 DB"),
        ("already_canonical", "已符合新布局（无需搬迁）"),
        ("db_only_resume", "目标 TXT 已到位（断点续迁补齐 DB/附属文件）"),
        ("missing_on_disk", "磁盘文件缺失（保持原样）"),
        ("conflict", "目标路径冲突（已跳过）"),
        ("outside_root", "路径越界或为空（已跳过）"),
    ):
        lines.append(f"  - {label}: {plan.action_counts.get(key, 0)}")

    lines.append("")
    lines.append("【待搬迁明细（按数据源与原路径分桶）】")
    for (source, bucket, action), cnt in sorted(plan.bucket_counts.items()):
        lines.append(f"  - source={source} | {bucket} | {action}: {cnt}")

    lines.append("")
    lines.append("【文件与目录变化预估】")
    lines.append(f"  - 待搬迁 .txt 正文: {plan.txt_move_count}")
    lines.append(f"  - 待搬迁 .epub: {plan.epub_move_count}")
    lines.append(f"  - 待搬迁图片资产（封面/插图）: {plan.asset_move_count}")
    lines.append(f"  - 需新建系列目录数: {len(plan.series_dirs_to_create)}")
    lines.append(f"  - 迁移后将彻底清空的旧目录数: {len(plan.dirs_becoming_empty)}")
    lines.append(f"  - 迁移后仍有非本次关联残留文件的旧目录数: {len(plan.dirs_with_leftovers)}")

    if plan.dirs_with_leftovers:
        lines.append("  - 残留文件旧目录示例:")
        for d in sorted(plan.dirs_with_leftovers)[:5]:
            leftovers = plan.dirs_with_leftovers[d]
            preview = ", ".join(leftovers[:3]) + (
                f" ... (+{len(leftovers) - 3})" if len(leftovers) > 3 else ""
            )
            lines.append(f"      {d}: {len(leftovers)} 个文件 ({preview})")

    if plan.root_unmanaged_files:
        preview_root = ", ".join(plan.root_unmanaged_files[:10])
        suffix = (
            f" ... (+{len(plan.root_unmanaged_files) - 10})" if len(plan.root_unmanaged_files) > 10 else ""
        )
        lines.append(f"  - 根目录非受管文件（原样保留）: {preview_root}{suffix}")

    if sample_limit > 0:
        lines.append("")
        lines.append(f"【迁移示例（每类最多展示 {sample_limit} 条）】")
        movables = [m for m in plan.moves if m.action in ("to_move", "db_only_resume")]
        sample_groups: list[tuple[str, list[PlannedMove]]] = [
            ("Pixiv 散篇", [m for m in movables if m.source == "pixiv" and not m.is_series]),
            ("Pixiv 系列", [m for m in movables if m.source == "pixiv" and m.is_series]),
            ("非 Pixiv 来源", [m for m in movables if m.source != "pixiv"]),
        ]
        for title, items in sample_groups:
            if not items:
                continue
            lines.append(f"  [{title}] (共 {len(items)} 条)")
            # Deduplicate by bucket/series so samples stay informative.
            shown = 0
            seen_series_or_dir: set[str] = set()
            for item in items:
                group_key = (
                    f"{item.source}:{Path(item.new_rel_path).parent}"
                    if item.is_series
                    else str(item.novel_id)
                )
                if item.is_series and group_key in seen_series_or_dir:
                    continue
                seen_series_or_dir.add(group_key)
                asset_note = f" (+{len(item.asset_moves)} 图)" if item.asset_moves else ""
                epub_note = " (+.epub)" if item.old_epub is not None else ""
                lines.append(
                    f"    #{item.novel_id}: {item.old_rel_path}  ->  {item.new_rel_path}{epub_note}{asset_note}"
                )
                shown += 1
                if shown >= sample_limit:
                    break

    anomalies = [m for m in plan.moves if m.action in ("conflict", "outside_root", "missing_on_disk")]
    if anomalies:
        lines.append("")
        lines.append(
            f"【异常 / 跳过示例（共 {len(anomalies)} 条，展示前 {min(sample_limit, len(anomalies))} 条）】"
        )
        for item in anomalies[:sample_limit]:
            lines.append(
                f"  - #{item.novel_id} [{item.action}] {item.reason}: {item.old_rel_path} -> {item.new_rel_path}"
            )

    if applied is not None:
        lines.append("")
        lines.append("【执行结果】")
        lines.append(f"  - 计划处理行数: {applied.attempted_rows}")
        lines.append(f"  - 成功更新 DB 行数: {applied.updated_db_rows}")
        lines.append(f"  - 实移 .txt: {applied.moved_txt}")
        lines.append(f"  - 实移 .epub: {applied.moved_epub}")
        lines.append(f"  - 实移图片资产: {applied.moved_assets}")
        lines.append(f"  - 清理空目录: {applied.removed_empty_dirs}")
        lines.append(f"  - 失败行数: {len(applied.failed_rows)}")
        for nid, err in applied.failed_rows[:10]:
            lines.append(f"      #{nid}: {err}")
    else:
        lines.append("")
        lines.append("提示：实际执行 `--apply` 搬迁前，建议先停止后台服务与下载任务以避免读写竞争。")

    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="将旧 download 目录结构平滑迁移为多数据源平等目录结构。")
    parser.add_argument(
        "--apply", action="store_true", help="真正移动文件并更新数据库 novel.path（默认仅 --dry-run）"
    )
    parser.add_argument("--batch-size", type=int, default=500, help="数据库批量更新每批提交条数（默认 500）")
    parser.add_argument("--sample", type=int, default=10, help="每类展示的迁移示例条数（默认 10）")
    parser.add_argument("--source", type=str, default=None, help="仅扫描指定来源（如 pixiv / esj / local）")
    parser.add_argument("--database-url", type=str, default=None, help="覆盖数据库连接 URL")
    parser.add_argument("--download-dir", type=str, default=None, help="覆盖下载根目录路径")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    cfg = load_config()
    db_url = args.database_url or cfg.database_url
    download_dir = args.download_dir or cfg.path.download

    engine = create_engine(db_url)
    try:
        rows = load_migration_rows(engine, source=args.source)
        plan = build_migration_plan(rows, download_dir=download_dir)
        applied = apply_migration_plan(plan, engine, batch_size=args.batch_size) if args.apply else None
        print(format_report(plan, sample_limit=args.sample, applied=applied))
        return 1 if applied and applied.failed_rows else 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
