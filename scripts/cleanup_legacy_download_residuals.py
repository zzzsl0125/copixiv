#!/usr/bin/env python3
"""Clean up legacy 4-digit shard residuals left in ``download/`` after layout migration.

Default mode is ``--dry-run`` (read-only scan and report). Pass ``--apply`` to:
1. Replace stale canonical targets with newer differing legacy copies (``overwrite_newer``);
2. Move legacy files whose canonical target is missing when expected (``move_missing``);
3. Delete redundant legacy duplicates (``delete_duplicate``) and orphan files not in DB (``delete_orphan``);
4. Remove emptied ``download/{4-digit}/`` subdirectories.

Only direct 4-digit numeric subdirectories (``^\\d{4}$``) under ``download_dir`` are inspected;
``pixiv/``, ``esj/``, ``local/``, and files at the ``download/`` root are never touched.
"""

from __future__ import annotations

import argparse
import errno
import os
import re
import shutil
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from sqlalchemy import select
from sqlalchemy.engine import Engine

from copixiv.config import load_config
from copixiv.db.engine import create_engine
from copixiv.db.models import Novel
from copixiv.storage.atomic import resolve_within_root

FOUR_DIGIT_DIR_RE = re.compile(r"^\d{4}$")
MANAGED_NOVEL_FILE_RE = re.compile(r"_(-?\d+)\.(txt|epub)$", re.IGNORECASE)

ResidualAction = Literal[
    "overwrite_newer",
    "move_missing",
    "delete_duplicate",
    "delete_orphan",
    "skip_unmanaged",
    "skip_anomaly",
]


@dataclass(frozen=True)
class NovelTargetRow:
    """Minimal DB row needed to verify legacy shard files against canonical paths."""

    id: int
    path: str | None
    has_epub: int


@dataclass(frozen=True)
class ResidualItem:
    """Planned filesystem action for a single entry inside a 4-digit legacy directory."""

    action: ResidualAction
    src: Path
    dst: Path | None = None
    novel_id: int | None = None
    ext: str = ""
    size_bytes: int = 0
    reason: str = ""


@dataclass
class CleanupPlan:
    """Complete read-only residual cleanup plan across ``download/{4-digit}/`` directories."""

    download_dir: str
    root: Path
    numeric_dirs_scanned: int = 0
    items: list[ResidualItem] = field(default_factory=list)
    action_counts: Counter[str] = field(default_factory=Counter)
    bytes_by_action: Counter[str] = field(default_factory=Counter)
    ext_counts: Counter[tuple[str, str]] = field(default_factory=Counter)
    dirs_becoming_empty: list[Path] = field(default_factory=list)
    dirs_kept_nonempty: dict[Path, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class CleanupApplyResult:
    """Execution summary returned by :func:`apply_cleanup_plan`."""

    overwritten_newer: int
    moved_missing: int
    deleted_duplicates: int
    deleted_orphans: int
    freed_bytes: int
    removed_empty_dirs: int
    failed_items: list[tuple[Path, str]]


def load_novel_targets(engine: Engine) -> dict[int, NovelTargetRow]:
    """Load ``{novel_id: NovelTargetRow}`` for all novels in the database."""
    stmt = select(Novel.id, Novel.path, Novel.has_epub)
    with engine.connect() as conn:
        return {
            int(row[0]): NovelTargetRow(
                id=int(row[0]),
                path=row[1],
                has_epub=int(row[2] or 0),
            )
            for row in conn.execute(stmt)
        }


def _file_sort_key(path: Path) -> tuple[float, int, str]:
    st = path.stat()
    return (st.st_mtime, st.st_size, path.name)


def _target_for_novel(row: NovelTargetRow, ext: str, root: Path) -> Path | None:
    raw_path = (row.path or "").strip()
    if not raw_path:
        return None
    resolved_txt = resolve_within_root(Path(raw_path), root)
    if resolved_txt is None:
        return None
    if ext == "txt":
        return resolved_txt
    return resolved_txt.with_suffix(".epub")


def build_cleanup_plan(
    novel_targets: dict[int, NovelTargetRow],
    download_dir: str = "download",
) -> CleanupPlan:
    """Scan direct 4-digit subdirectories under *download_dir* and build a cleanup plan."""
    root = Path(download_dir).resolve()
    plan = CleanupPlan(download_dir=download_dir, root=root)
    if not root.is_dir():
        return plan

    numeric_dirs = sorted(
        (
            entry_path.resolve()
            for entry in os.scandir(root)
            if entry.is_dir(follow_symlinks=False) and FOUR_DIGIT_DIR_RE.fullmatch(entry.name)
            for entry_path in (Path(entry.path),)
        ),
        key=lambda p: p.name,
    )
    numeric_dir_set = set(numeric_dirs)
    plan.numeric_dirs_scanned = len(numeric_dirs)

    grouped_in_db: dict[tuple[int, str], list[Path]] = defaultdict(list)
    dir_unmanaged_entries: dict[Path, list[str]] = defaultdict(list)

    for shard_dir in numeric_dirs:
        try:
            with os.scandir(shard_dir) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            dir_unmanaged_entries[shard_dir].append(f"<读取目录失败: {exc}>")
            continue

        for entry in entries:
            entry_path = Path(entry.path).resolve()
            try:
                is_regular = entry.is_file(follow_symlinks=False)
            except OSError:
                is_regular = False
            if not is_regular:
                item = ResidualItem(
                    action="skip_unmanaged",
                    src=entry_path,
                    reason="非普通文件（目录或符号链接）",
                )
                plan.items.append(item)
                plan.action_counts["skip_unmanaged"] += 1
                dir_unmanaged_entries[shard_dir].append(entry.name)
                continue

            match = MANAGED_NOVEL_FILE_RE.search(entry.name)
            if match is None:
                size_b = entry_path.stat().st_size
                item = ResidualItem(
                    action="skip_unmanaged",
                    src=entry_path,
                    size_bytes=size_b,
                    reason="文件名不符合受管作品格式",
                )
                plan.items.append(item)
                plan.action_counts["skip_unmanaged"] += 1
                plan.bytes_by_action["skip_unmanaged"] += size_b
                dir_unmanaged_entries[shard_dir].append(entry.name)
                continue

            nid = int(match.group(1))
            ext = match.group(2).lower()
            size_b = entry_path.stat().st_size

            if nid not in novel_targets:
                item = ResidualItem(
                    action="delete_orphan",
                    src=entry_path,
                    novel_id=nid,
                    ext=ext,
                    size_bytes=size_b,
                    reason="作品 ID 不在数据库中",
                )
                plan.items.append(item)
                plan.action_counts["delete_orphan"] += 1
                plan.bytes_by_action["delete_orphan"] += size_b
                plan.ext_counts[("delete_orphan", ext)] += 1
                continue

            grouped_in_db[(nid, ext)].append(entry_path)

    for (nid, ext), legacy_files in sorted(grouped_in_db.items()):
        row = novel_targets[nid]
        target = _target_for_novel(row, ext, root)
        if target is None or target.parent in numeric_dir_set:
            reason = (
                "DB path 为空或落在下载根目录之外"
                if target is None
                else "DB path 仍指向旧 4 位分片目录（未迁移）"
            )
            for src in legacy_files:
                size_b = src.stat().st_size
                plan.items.append(
                    ResidualItem(
                        action="skip_anomaly",
                        src=src,
                        dst=target,
                        novel_id=nid,
                        ext=ext,
                        size_bytes=size_b,
                        reason=reason,
                    )
                )
                plan.action_counts["skip_anomaly"] += 1
                plan.bytes_by_action["skip_anomaly"] += size_b
                dir_unmanaged_entries[src.parent].append(src.name)
            continue

        best = max(legacy_files, key=_file_sort_key)
        best_stat = best.stat()

        if target.exists():
            target_stat = target.stat()
            if best_stat.st_mtime > target_stat.st_mtime and best.read_bytes() != target.read_bytes():
                plan.items.append(
                    ResidualItem(
                        action="overwrite_newer",
                        src=best,
                        dst=target,
                        novel_id=nid,
                        ext=ext,
                        size_bytes=best_stat.st_size,
                        reason="旧目录版本比现行目标更新且内容不同",
                    )
                )
                plan.action_counts["overwrite_newer"] += 1
                plan.bytes_by_action["overwrite_newer"] += best_stat.st_size
                plan.ext_counts[("overwrite_newer", ext)] += 1
                redundant = [f for f in legacy_files if f != best]
            else:
                redundant = list(legacy_files)
        else:
            if ext == "txt" or row.has_epub == 2:
                plan.items.append(
                    ResidualItem(
                        action="move_missing",
                        src=best,
                        dst=target,
                        novel_id=nid,
                        ext=ext,
                        size_bytes=best_stat.st_size,
                        reason="现行目标文件缺失，恢复旧目录最新版本",
                    )
                )
                plan.action_counts["move_missing"] += 1
                plan.bytes_by_action["move_missing"] += best_stat.st_size
                plan.ext_counts[("move_missing", ext)] += 1
                redundant = [f for f in legacy_files if f != best]
            else:
                redundant = list(legacy_files)

        for dup in redundant:
            size_b = dup.stat().st_size
            plan.items.append(
                ResidualItem(
                    action="delete_duplicate",
                    src=dup,
                    dst=target,
                    novel_id=nid,
                    ext=ext,
                    size_bytes=size_b,
                    reason="已有对应规范目标文件，删除冗余副本",
                )
            )
            plan.action_counts["delete_duplicate"] += 1
            plan.bytes_by_action["delete_duplicate"] += size_b
            plan.ext_counts[("delete_duplicate", ext)] += 1

    for shard_dir in numeric_dirs:
        kept = sorted(dir_unmanaged_entries.get(shard_dir, ()))
        if kept:
            plan.dirs_kept_nonempty[shard_dir] = kept
        else:
            plan.dirs_becoming_empty.append(shard_dir)

    return plan


def _replace_file_atomic(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        fd, tmp_name = tempfile.mkstemp(prefix=".cleanup_", dir=dst.parent)
        os.close(fd)
        tmp_path = Path(tmp_name)
        try:
            shutil.copy2(src, tmp_path)
            os.replace(tmp_path, dst)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        src.unlink()


def apply_cleanup_plan(plan: CleanupPlan) -> CleanupApplyResult:
    """Execute planned overwrites/moves first, then delete duplicates/orphans and empty shard dirs."""
    overwritten_newer = 0
    moved_missing = 0
    deleted_duplicates = 0
    deleted_orphans = 0
    freed_bytes = 0
    failed_items: list[tuple[Path, str]] = []

    promotions = [item for item in plan.items if item.action in ("overwrite_newer", "move_missing")]
    deletions = [item for item in plan.items if item.action in ("delete_duplicate", "delete_orphan")]

    for item in promotions:
        if item.dst is None:
            failed_items.append((item.src, "缺少目标路径"))
            continue
        try:
            _replace_file_atomic(item.src, item.dst)
            if item.action == "overwrite_newer":
                overwritten_newer += 1
            else:
                moved_missing += 1
        except OSError as exc:
            failed_items.append((item.src, str(exc)))

    for item in deletions:
        try:
            item.src.unlink()
            freed_bytes += item.size_bytes
            if item.action == "delete_duplicate":
                deleted_duplicates += 1
            else:
                deleted_orphans += 1
        except OSError as exc:
            failed_items.append((item.src, str(exc)))

    removed_empty_dirs = 0
    for shard_dir in plan.dirs_becoming_empty:
        resolved = shard_dir.resolve()
        if resolve_within_root(resolved, plan.root) is None or resolved == plan.root:
            continue
        try:
            if resolved.is_dir() and not resolved.is_symlink() and not any(resolved.iterdir()):
                resolved.rmdir()
                removed_empty_dirs += 1
        except OSError as exc:
            failed_items.append((resolved, f"删除空目录失败: {exc}"))

    return CleanupApplyResult(
        overwritten_newer=overwritten_newer,
        moved_missing=moved_missing,
        deleted_duplicates=deleted_duplicates,
        deleted_orphans=deleted_orphans,
        freed_bytes=freed_bytes,
        removed_empty_dirs=removed_empty_dirs,
        failed_items=failed_items,
    )


def _fmt_mb(size_bytes: int) -> str:
    return f"{size_bytes / (1024 * 1024):.2f} MB"


def format_report(
    plan: CleanupPlan,
    *,
    sample_limit: int = 10,
    applied: CleanupApplyResult | None = None,
) -> str:
    """Format a human-readable dry-run or apply summary report."""
    lines: list[str] = []
    mode = "执行模式 (--apply)" if applied is not None else "试运行预览 (--dry-run，未修改任何文件)"
    lines.append(f"=== download 旧分片残留清理报告 [{mode}] ===")
    lines.append(f"下载根目录: {plan.root}（配置项: {plan.download_dir}）")
    lines.append(f"扫描 4 位旧分片目录数: {plan.numeric_dirs_scanned}")
    lines.append(f"发现受管/非受管条目总数: {len(plan.items)}")
    lines.append("")
    lines.append("【处理动作汇总】")
    for key, label in (
        ("overwrite_newer", "旧版更新 → 覆盖现行目标（保留最新版）"),
        ("move_missing", "现行缺失 → 补回现行目标"),
        ("delete_duplicate", "冗余重复副本 → 删除旧副本"),
        ("delete_orphan", "库中不存在的孤儿文件 → 删除"),
        ("skip_unmanaged", "非受管文件/目录 → 原样保留"),
        ("skip_anomaly", "异常条目 → 已跳过"),
    ):
        cnt = plan.action_counts.get(key, 0)
        size_b = plan.bytes_by_action.get(key, 0)
        txt_cnt = plan.ext_counts.get((key, "txt"), 0)
        epub_cnt = plan.ext_counts.get((key, "epub"), 0)
        ext_detail = f" (txt={txt_cnt}, epub={epub_cnt})" if (txt_cnt or epub_cnt) else ""
        lines.append(f"  - {label}: {cnt}{ext_detail}（{_fmt_mb(size_b)}）")

    lines.append("")
    lines.append("【目录清理预估】")
    lines.append(f"  - 清理后将清空并删除的旧分片目录数: {len(plan.dirs_becoming_empty)}")
    lines.append(f"  - 仍有保留条目需留存的旧分片目录数: {len(plan.dirs_kept_nonempty)}")
    total_reclaim = plan.bytes_by_action.get("delete_duplicate", 0) + plan.bytes_by_action.get(
        "delete_orphan", 0
    )
    lines.append(f"  - 预计净释放空间（仅计直接删除）: {_fmt_mb(total_reclaim)}")

    if sample_limit > 0:
        for action_key, title in (
            ("overwrite_newer", "覆盖升级示例"),
            ("move_missing", "缺失补回示例"),
            ("delete_duplicate", "删除冗余副本示例"),
            ("delete_orphan", "删除孤儿文件示例"),
            ("skip_unmanaged", "非受管跳过示例"),
            ("skip_anomaly", "异常跳过示例"),
        ):
            matched = [it for it in plan.items if it.action == action_key]
            if not matched:
                continue
            lines.append("")
            lines.append(f"【{title}（共 {len(matched)} 条，展示前 {min(sample_limit, len(matched))} 条）】")
            for it in matched[:sample_limit]:
                if it.dst is not None and action_key in ("overwrite_newer", "move_missing"):
                    lines.append(f"  - #{it.novel_id}: {it.src} -> {it.dst}")
                elif it.novel_id is not None:
                    lines.append(f"  - #{it.novel_id}: {it.src} ({it.reason})")
                else:
                    lines.append(f"  - {it.src} ({it.reason})")

    if applied is not None:
        lines.append("")
        lines.append("【执行结果】")
        lines.append(f"  - 覆盖升级文件: {applied.overwritten_newer}")
        lines.append(f"  - 缺失补回文件: {applied.moved_missing}")
        lines.append(f"  - 删除冗余副本: {applied.deleted_duplicates}")
        lines.append(f"  - 删除孤儿文件: {applied.deleted_orphans}")
        lines.append(f"  - 释放空间: {_fmt_mb(applied.freed_bytes)}")
        lines.append(f"  - 删除空分片目录: {applied.removed_empty_dirs}")
        lines.append(f"  - 失败条目数: {len(applied.failed_items)}")
        for path, err in applied.failed_items[:10]:
            lines.append(f"      {path}: {err}")
    else:
        lines.append("")
        lines.append("提示：确认以上预览统计无误后，追加 `--apply` 执行真实清理。")

    return "\n".join(lines)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="清理旧 download/{4位分片}/ 目录中的遗留孤儿与重复文件。")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正执行覆盖、删除并清理空目录（默认仅 --dry-run）",
    )
    parser.add_argument("--sample", type=int, default=10, help="每类展示的示例条数（默认 10）")
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
        novel_targets = load_novel_targets(engine)
    finally:
        engine.dispose()

    plan = build_cleanup_plan(novel_targets, download_dir=download_dir)
    applied = apply_cleanup_plan(plan) if args.apply else None
    print(format_report(plan, sample_limit=args.sample, applied=applied))
    return 1 if applied and applied.failed_items else 0


if __name__ == "__main__":
    sys.exit(main())
