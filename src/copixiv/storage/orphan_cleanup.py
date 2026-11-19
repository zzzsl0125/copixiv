"""Filesystem orphan-file detection and reclamation (storage-level utilities)."""

from __future__ import annotations

import time
from pathlib import Path

from copixiv.log import logger
from copixiv.storage import assets

EXPORT_FILE_PREFIX = "batch_export_"


def extract_managed_novel_id(name_stem: str) -> int | None:
    """Extract the owning novel ID from a managed download filename stem."""
    if name_stem.startswith(EXPORT_FILE_PREFIX):
        return None
    asset_novel_id = assets.novel_id_of(name_stem)
    if asset_novel_id is not None:
        return asset_novel_id

    tokens = name_stem.split("_")
    if tokens[-1].isdigit():
        return int(tokens[-1])
    return None


def collect_orphan_files(
    root: Path,
    known_ids: set[int],
    min_age_days: int,
    now: float | None = None,
) -> list[tuple[Path, int]]:
    """Walk *root* and return ``[(path, size_bytes), ...]`` of reclaimable files."""
    resolved_root = root.resolve()
    current_time = time.time() if now is None else now
    orphans: list[tuple[Path, int]] = []
    min_age_seconds = min_age_days * 86400

    for dirpath, _dirnames, filenames in resolved_root.walk():
        for name in filenames:
            path = dirpath / name
            if path.suffix == ".tmp":
                continue
            try:
                if path.is_symlink() and not path.resolve().is_relative_to(resolved_root):
                    continue
                st = path.stat()
            except OSError:
                continue

            if (current_time - st.st_mtime) < min_age_seconds:
                continue

            novel_id = extract_managed_novel_id(path.stem)
            if novel_id is None or novel_id in known_ids:
                continue
            orphans.append((path, st.st_size))

    return orphans


def unlink_files(paths: list[Path]) -> tuple[int, int]:
    """Delete *paths*; return ``(deleted_count, freed_bytes)``."""
    deleted = 0
    freed = 0
    for path in paths:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
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
