"""``prune_orphan_files`` — reclaim files on disk that no row points at.

**Why this exists.**  Deleting a novel is deliberately ordered "DB first, files
second" (``DeleteNovelUseCase``) and the file half is *best-effort*: an OSError,
a permission problem, or a crash between the two leaves the row gone and the
file behind.  Nothing re-checks that, so orphans only accumulate.  Measured on
production (2026-10-24):

| kind | count | size |
|---|---:|---:|
| ``.txt`` no row points at | **4 130** | **133.3 MB** |
| ``.epub`` no row points at | 142 | — |
| stray images (``_u_*`` / ``_p_*`` leftovers) | 40 | — |

3 877 of those text files are older than 180 days, so this is not a live-work
race — it is dead weight from deletions that half-failed months ago.

**Why it is a separate task and not part of ``check_epub``.**  ``check_epub``
walks *rows* and asks "is the file right?"; this walks *files* and asks "is
there a row?" — the opposite direction, with the opposite failure mode
(deleting a file a row still needs is destructive; classifying a row that has
no file is not).  Keeping them apart means a bug here cannot corrupt the EPUB
state machine, and each can be run/skipped independently.

**Safety is the whole design.**  A file is only removed when *all* of these
hold:

1. it sits **inside** the download root (the same containment rule
   ``FileStorage.delete_novel_files`` uses — a polluted root must not become an
   arbitrary delete).  Symlinks are resolved *before* the check, and a symlink
   pointing outside the root is skipped rather than followed.
2. its ``<id>`` suffix parses, and **no** ``novel`` row has that id;
3. it is older than ``--min-age-days`` (default 7).  A file whose row is being
   written right now — plan → download → persist means the body lands *before*
   the row — must never be reclaimed mid-flight;
4. ``--dry-run`` is off.  The default **is** dry-run: this task reports first,
   and only an explicit ``--apply`` deletes.  Same shape as
   ``scripts/repair_epub.py``, and for the same reason: an operator wants to
   see the list before it is gone.

The ledger is *not* consulted: a ``failed_novel`` row (or any other record)
does not justify keeping a body file — the ledger describes a download attempt,
never an artifact on disk.

**How to run it.**  Registered as ``prune_orphan_files`` with an args model
(``apply`` / ``min_age_days``), so it can be scheduled, but the recommended
first run is the dry run through the task page or ``scripts/`` — the summary
reports the would-be count and byte size.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy import select as _select

from copixiv.core.models import TaskResult
from copixiv.db import models
from copixiv.log import logger

from ..kernel import TaskContext
from ..kernel import register


class PruneOrphanFilesArgs(BaseModel):
    """Parameters for ``prune_orphan_files``.

    ``apply=False`` is the default on purpose: the task reports what it would
    delete, and deleting requires an explicit opt-in.  A scheduled run of this
    task therefore does nothing but count — to actually reclaim, set
    ``apply: true`` in the schedule's params (or run it once by hand).
    """

    apply: bool = Field(
        default=False,
        description="真正删除。默认 false = 只统计不删（试运行）",
    )
    min_age_days: int = Field(
        default=7, ge=0,
        description=(
            "只处理超过这么多天没改动的文件。默认 7：正在下载的书先写文件、"
            "后写行，太小会误删在途作品"
        ),
    )


#: Prefix of the batch-export ZIPs that live in the same directory.  They are
#: **not** novel artifacts: ``tasks/batch.py`` writes them next to the library
#: (``batch_export_<task_id>.zip``) so the browser can fetch the finished
#: archive, and sweeps them itself after 24 h.  Parsing their trailing number
#: as a novel id would make this task delete a 320 MB export that a user is
#: currently downloading — measured on production, the two exports alone are
#: 594 MB of the tree.
EXPORT_FILE_PREFIX = "batch_export_"


def _id_of(name_stem: str) -> int | None:
    """The novel id a managed filename belongs to, or ``None`` if unmanaged.

    Three naming shapes live in the download tree (``core/naming.py`` and
    ``storage/image_downloader.py``):

    * ``{safe_title}_{id}``            — a novel's text / EPUB
    * ``{id}_u_{image_id}``            — an uploaded (inline) image
    * ``{id}_c_cover``                 — the cover

    Assets carry the id in the **first** token, novels in the **last**; both
    are accepted, because getting this wrong is what turns "clean up orphans"
    into "delete a real book".  Export archives are rejected up front (see
    :data:`EXPORT_FILE_PREFIX`).  A name that parses as none of these is not
    ours and is left alone.
    """
    if name_stem.startswith(EXPORT_FILE_PREFIX):
        return None
    tokens = name_stem.split("_")

    # Assets first: ``{id}_u_{image_id}`` / ``{id}_c_cover``.  Their *last*
    # token is an image id or a literal ("cover", "u"), never the novel id, so
    # the novel rule below must not see them.  Getting this order wrong reads
    # a novel id out of ``…_u_25414623`` as 25414623 and reports a live book's
    # illustrations as orphans — i.e. it deletes them.
    if "u" in tokens[1:2] or "c" in tokens[1:2]:
        return int(tokens[0]) if tokens[0].isdigit() else None

    # Novels: ``{safe_title}_{id}`` -- the id is the trailing token.
    if tokens[-1].isdigit():
        return int(tokens[-1])
    return None


def _collect_orphans(
    root: Path, known_ids: set[int], min_age_days: int, now: float,
) -> list[tuple[Path, int]]:
    """Walk *root* and return ``[(path, size), ...]`` of reclaimable files.

    Pure filesystem work (no DB), so it runs in a worker thread.  The
    ``known_ids`` snapshot is passed in rather than queried here, which keeps
    this function independently testable.
    """
    orphans: list[tuple[Path, int]] = []
    max_age_seconds = min_age_days * 86400

    for dirpath, _dirnames, filenames in root.walk():
        for name in filenames:
            path = dirpath / name
            # ``.tmp`` files belong to in-flight atomic writes; they are the
            # writer's business, never ours.
            if path.suffix == ".tmp":
                continue
            try:
                # A symlink is only acceptable when it stays inside the root;
                # otherwise skip it rather than delete through it.
                if path.is_symlink() and not path.resolve().is_relative_to(root):
                    continue
                st = path.stat()
            except OSError:
                continue

            if (now - st.st_mtime) < max_age_seconds:
                continue

            novel_id = _id_of(path.stem)
            if novel_id is None:
                continue                      # unmanaged name — not ours
            if novel_id in known_ids:
                continue
            orphans.append((path, st.st_size))

    return orphans


def _unlink(paths: list[Path]) -> tuple[int, int]:
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


@register(
    "prune_orphan_files",
    description="清理磁盘上没有任何小说行指向的遗留文件（默认只统计不删）",
    args=PruneOrphanFilesArgs,
)
async def prune_orphan_files(
    args: PruneOrphanFilesArgs, ctx: TaskContext,
) -> TaskResult:
    """Reclaim files left behind by deletions that did not finish.

    Read-only by default: ``apply=False`` reports what it *would* delete.
    See the module docstring for the four safety conditions.
    """
    if ctx.file_storage is None:
        return TaskResult(summary="孤儿文件清理: 未配置文件存储，跳过")

    root = Path(ctx.file_storage.download_dir).resolve()
    if not root.is_dir():
        return TaskResult(summary=f"孤儿文件清理: 下载目录不存在 {root}")

    with ctx.session_factory() as session:
        known_ids = set(
            session.execute(_select(models.Novel.id)).scalars().all()
        )

    now = time.time()
    orphans = await asyncio.to_thread(
        _collect_orphans, root, known_ids, args.min_age_days, now,
    )
    total_bytes = sum(size for _path, size in orphans)
    size_mb = total_bytes / (1024 * 1024)

    if not orphans:
        return TaskResult(
            summary=(
                f"孤儿文件清理: 没有可回收的文件"
                f"（{len(known_ids)} 本书全部有主）"
            )
        )

    if not args.apply:
        logger.info(
            f"prune_orphan_files: 试运行，可回收 {len(orphans)} 个文件 "
            f"{size_mb:.1f} MB（加 apply=true 才真正删除）"
        )
        return TaskResult(
            summary=(
                f"孤儿文件清理（试运行）: 可回收 {len(orphans)} 个文件、"
                f"{size_mb:.1f} MB；未删除任何文件"
                f"（设 apply=true 执行）"
            )
        )

    deleted, freed = await asyncio.to_thread(
        _unlink, [p for p, _size in orphans],
    )
    logger.info(
        f"prune_orphan_files: 删除 {deleted}/{len(orphans)} 个孤儿文件，"
        f"释放 {freed / (1024 * 1024):.1f} MB"
    )
    return TaskResult(
        summary=(
            f"孤儿文件清理: 删除 {deleted} 个文件、"
            f"释放 {freed / (1024 * 1024):.1f} MB"
            + (f"（{len(orphans) - deleted} 个删除失败，见日志）"
               if deleted < len(orphans) else "")
        )
    )


__all__ = [
    "PruneOrphanFilesArgs",
    "prune_orphan_files",
]
