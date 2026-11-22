"""Filesystem orphan-file detection and reclamation (storage-level utilities)."""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
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
    if last.isdigit() or (last.startswith("-") and len(last) > 1 and last[1:].isdigit()):
        return int(last)
    return None


def collect_orphan_files(
    root: Path,
    known_ids: set[int],
    min_age_days: int,
    now: float | None = None,
    known_paths: set[Path] | None = None,
) -> list[tuple[Path, int]]:
    """Walk *root* and return ``[(path, size_bytes), ...]`` of reclaimable files."""
    resolved_root = root.resolve()
    normalized_known_paths = {p.resolve() for p in known_paths} if known_paths else set()
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

            if normalized_known_paths and entry_path in normalized_known_paths:
                continue

            is_export = name.startswith(EXPORT_FILE_PREFIX) and entry_path.suffix == ".zip"
            if not is_export:
                novel_id = extract_managed_novel_id(entry_path.stem)
                if novel_id is None or novel_id in known_ids:
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


__all__ = [
    "EXPORT_FILE_PREFIX",
    "collect_orphan_files",
    "extract_managed_novel_id",
    "unlink_files",
]
