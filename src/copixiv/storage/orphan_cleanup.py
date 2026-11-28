"""Filesystem orphan-file detection and reclamation (storage-level utilities)."""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from copixiv.log import logger
from copixiv.storage import assets
from copixiv.storage.atomic import resolve_within_root

EXPORT_FILE_PREFIX = "batch_export_"


def extract_managed_novel_id(name_stem: str) -> int | None:
    """Extract the owning novel ID from a managed download filename stem."""
    if name_stem.startswith(EXPORT_FILE_PREFIX):
        return None
    asset_novel_id = assets.novel_id_of(name_stem)
    if asset_novel_id is not None:
        return asset_novel_id

    tokens = name_stem.split("_")
    last = tokens[-1]
    if assets.is_novel_id_str(last):
        return int(last)
    return None


def collect_orphan_files(
    root: Path,
    known_paths: set[Path],
    min_age_days: int,
    now: float | None = None,
) -> list[tuple[Path, int]]:
    """Walk *root* and return ``[(path, size_bytes), ...]`` of reclaimable files.

    Files are kept if they match *known_paths* (a DB novel's text path or its
    sibling ``.epub``) or if their stem does not follow copixiv's managed
    naming convention.
    """
    resolved_root = root.resolve()
    normalized_known_paths = {p.resolve() for p in known_paths}
    current_time = time.time() if now is None else now
    orphans: list[tuple[Path, int]] = []
    min_age_seconds = min_age_days * 86400

    stack: list[Path] = [resolved_root]
    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as it:
                entries = list(it)
        except OSError:
            continue

        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    continue

                if entry.is_symlink():
                    entry_path = Path(entry.path)
                    if resolve_within_root(entry_path, resolved_root) is None:
                        continue
                    if not entry_path.is_file():
                        continue
                elif not entry.is_file(follow_symlinks=False):
                    continue
                else:
                    entry_path = Path(entry.path)
            except OSError:
                continue

            name = entry.name
            if entry_path.suffix == ".tmp":
                continue

            if entry_path in normalized_known_paths:
                continue

            is_export = name.startswith(EXPORT_FILE_PREFIX) and entry_path.suffix == ".zip"
            if not is_export and extract_managed_novel_id(entry_path.stem) is None:
                continue

            try:
                st = entry.stat()
            except OSError:
                continue

            if (current_time - st.st_mtime) < min_age_seconds:
                continue

            orphans.append((entry_path, st.st_size))

    return orphans


def unlink_files(files: Sequence[tuple[Path, int]]) -> tuple[int, int]:
    """Delete *files*; return ``(deleted_count, freed_bytes)``."""
    deleted = 0
    freed = 0
    for path, size in files:
        try:
            path.unlink()
        except OSError as exc:
            logger.warning(f"prune_orphan_files: 删除失败 {path}: {exc}")
            continue
        deleted += 1
        freed += size
    return deleted, freed


@dataclass
class _DirScanInfo:
    child_dirs: list[Path]
    has_blocking_entry: bool
    deleted_direct_files: bool
    mtime: float


def collect_empty_dirs(
    root: Path,
    *,
    protected_top_dirs: Iterable[str] = (),
    min_age_days: int = 7,
    now: float | None = None,
    assumed_deleted: Iterable[Path] = (),
) -> list[Path]:
    """Return removable empty directories under *root* ordered deepest-first.

    Directories containing files in *assumed_deleted* (or removable child subdirs)
    become eligible immediately; already-empty directories must be at least
    *min_age_days* old. *root* itself and top-level dirs in *protected_top_dirs*
    are never returned.
    """
    resolved_root = root.resolve()
    if not resolved_root.is_dir():
        return []

    current_time = time.time() if now is None else now
    min_age_seconds = min_age_days * 86400
    protected_set = {name.strip() for name in protected_top_dirs if isinstance(name, str) and name.strip()}
    normalized_deleted = {p.resolve() for p in assumed_deleted}

    scanned: dict[Path, _DirScanInfo] = {}
    stack: list[Path] = [resolved_root]
    while stack:
        current_dir = stack.pop()
        if current_dir in scanned:
            continue
        try:
            dir_stat = os.stat(current_dir, follow_symlinks=False)
            with os.scandir(current_dir) as it:
                entries = list(it)
        except OSError:
            continue

        child_dirs: list[Path] = []
        has_blocking_entry = False
        deleted_direct_files = False
        for entry in entries:
            try:
                if entry.is_symlink():
                    has_blocking_entry = True
                    continue
                if entry.is_dir(follow_symlinks=False):
                    child_path = Path(entry.path).resolve()
                    child_dirs.append(child_path)
                    stack.append(child_path)
                    continue
                if entry.is_file(follow_symlinks=False):
                    if Path(entry.path).resolve() in normalized_deleted:
                        deleted_direct_files = True
                    else:
                        has_blocking_entry = True
                    continue
                has_blocking_entry = True
            except OSError:
                has_blocking_entry = True

        scanned[current_dir] = _DirScanInfo(
            child_dirs=child_dirs,
            has_blocking_entry=has_blocking_entry,
            deleted_direct_files=deleted_direct_files,
            mtime=dir_stat.st_mtime,
        )

    removable_set: set[Path] = set()
    had_deleted_descendant: set[Path] = set()
    removable_dirs: list[Path] = []

    for current_dir in sorted(scanned, key=lambda p: (-len(p.parts), str(p))):
        info = scanned[current_dir]
        had_deleted = info.deleted_direct_files or any(
            child in had_deleted_descendant for child in info.child_dirs
        )
        if had_deleted:
            had_deleted_descendant.add(current_dir)

        if current_dir == resolved_root:
            continue
        if current_dir.parent == resolved_root and current_dir.name in protected_set:
            continue
        if info.has_blocking_entry:
            continue
        if any(child not in removable_set for child in info.child_dirs):
            continue
        if not had_deleted and (current_time - info.mtime) < min_age_seconds:
            continue

        removable_set.add(current_dir)
        removable_dirs.append(current_dir)

    return removable_dirs


def remove_empty_dirs(
    dirs: Sequence[Path],
    *,
    root: Path,
    protected_top_dirs: Iterable[str] = (),
) -> int:
    """Remove empty directories in *dirs* deepest-first, guarding *root* and top-level source dirs."""
    resolved_root = root.resolve()
    protected_set = {name.strip() for name in protected_top_dirs if isinstance(name, str) and name.strip()}
    removed = 0

    for candidate in sorted({d.resolve() for d in dirs}, key=lambda p: (-len(p.parts), str(p))):
        if candidate == resolved_root:
            continue
        if resolve_within_root(candidate, resolved_root) is None:
            continue
        if candidate.parent == resolved_root and candidate.name in protected_set:
            continue
        try:
            if not candidate.is_dir() or candidate.is_symlink():
                continue
            if any(candidate.iterdir()):
                continue
            candidate.rmdir()
            removed += 1
        except OSError as exc:
            logger.warning(f"prune_orphan_files: 删除空目录失败 {candidate}: {exc}")
            continue
    return removed


__all__ = [
    "EXPORT_FILE_PREFIX",
    "collect_empty_dirs",
    "collect_orphan_files",
    "extract_managed_novel_id",
    "remove_empty_dirs",
    "unlink_files",
]
